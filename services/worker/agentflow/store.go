package agentflow

import (
	"errors"
	"fmt"
	"sync"
	"time"

	"github.com/Disesfgewu/agentkit/graph"
)

// SPEC 12.3 lease profile.
const (
	LeaseTTL          = 60 * time.Second
	HeartbeatInterval = 15 * time.Second
)

var (
	ErrLeaseHeld        = errors.New("thread has a live lease")
	ErrFenced           = errors.New("fenced: lease token is stale or expired")
	ErrRevisionMismatch = errors.New("graph revision mismatch: not resumed from an old LastNode")
	ErrUnknownThread    = errors.New("unknown thread")
)

// Lease grants one worker the right to advance one thread's checkpoint.
// Token is a fencing token: it only grows, so a worker holding an older
// token can never write after another worker took over.
type Lease struct {
	ThreadID string
	Worker   string
	Token    int64
	Expires  time.Time
}

// CheckpointRecord is one saved checkpoint with its LAB metadata (SPEC 12.3:
// graph revision, state schema version, fencing token, increasing sequence).
type CheckpointRecord struct {
	Sequence      int64
	Token         int64
	Worker        string
	GraphRevision string
	SchemaVersion int
	Codec         string
	StateHash     string
	LastNode      string
	Bytes         []byte
}

type threadRow struct {
	projectID     string
	graphRevision string
	token         int64
	worker        string
	leaseExpires  time.Time
	records       []CheckpointRecord
}

// MemoryCheckpointDB is the in-memory reference for the LAB checkpoint
// tables. PostgresGraphStore (after #5's schema) must pass the same tests;
// this type exists so the semantics are fixed before the SQL is.
type MemoryCheckpointDB struct {
	mu      sync.Mutex
	now     func() time.Time
	threads map[string]*threadRow
}

func NewMemoryCheckpointDB(now func() time.Time) *MemoryCheckpointDB {
	if now == nil {
		now = time.Now
	}
	return &MemoryCheckpointDB{now: now, threads: map[string]*threadRow{}}
}

// CreateThread registers a globally unique thread bound to a project and the
// graph revision it runs.
func (db *MemoryCheckpointDB) CreateThread(threadID, projectID, graphRevision string) error {
	if threadID == "" || projectID == "" || graphRevision == "" {
		return errors.New("thread, project and graph revision are required")
	}
	db.mu.Lock()
	defer db.mu.Unlock()
	if _, ok := db.threads[threadID]; ok {
		return fmt.Errorf("thread %s already exists", threadID)
	}
	db.threads[threadID] = &threadRow{projectID: projectID, graphRevision: graphRevision}
	return nil
}

// Acquire grants a new lease if none is live; the fencing token increments.
func (db *MemoryCheckpointDB) Acquire(threadID, worker string) (Lease, error) {
	db.mu.Lock()
	defer db.mu.Unlock()
	row, ok := db.threads[threadID]
	if !ok {
		return Lease{}, ErrUnknownThread
	}
	now := db.now()
	if row.token > 0 && now.Before(row.leaseExpires) {
		return Lease{}, fmt.Errorf("%w (worker %s until %s)", ErrLeaseHeld, row.worker, row.leaseExpires.Format(time.RFC3339))
	}
	row.token++
	row.worker = worker
	row.leaseExpires = now.Add(LeaseTTL)
	return Lease{ThreadID: threadID, Worker: worker, Token: row.token, Expires: row.leaseExpires}, nil
}

// check must hold db.mu.
func (db *MemoryCheckpointDB) check(l Lease) (*threadRow, error) {
	row, ok := db.threads[l.ThreadID]
	if !ok {
		return nil, ErrUnknownThread
	}
	if l.Token != row.token || !db.now().Before(row.leaseExpires) {
		return nil, fmt.Errorf("%w (token %d, current %d)", ErrFenced, l.Token, row.token)
	}
	return row, nil
}

// Heartbeat extends a still-valid lease.
func (db *MemoryCheckpointDB) Heartbeat(l Lease) (Lease, error) {
	db.mu.Lock()
	defer db.mu.Unlock()
	row, err := db.check(l)
	if err != nil {
		return Lease{}, err
	}
	row.leaseExpires = db.now().Add(LeaseTTL)
	l.Expires = row.leaseExpires
	return l, nil
}

// Records returns the saved checkpoints of a thread, oldest first.
func (db *MemoryCheckpointDB) Records(threadID string) []CheckpointRecord {
	db.mu.Lock()
	defer db.mu.Unlock()
	row, ok := db.threads[threadID]
	if !ok {
		return nil
	}
	return append([]CheckpointRecord(nil), row.records...)
}

// Store returns an agentKit graph.Store bound to one lease and the graph
// revision the caller runs.
func (db *MemoryCheckpointDB) Store(l Lease, graphRevision string) graph.Store {
	return &fencedStore{db: db, lease: l, revision: graphRevision}
}

type fencedStore struct {
	db       *MemoryCheckpointDB
	lease    Lease
	revision string
}

func (s *fencedStore) Save(threadID string, c graph.Checkpoint) error {
	if threadID != s.lease.ThreadID {
		return fmt.Errorf("lease is for thread %s, not %s", s.lease.ThreadID, threadID)
	}
	if err := ValidateEnvelope(c.State); err != nil {
		return err
	}
	b, err := EncodeCheckpoint(c)
	if err != nil {
		return err
	}
	hash, err := StateHash(c.State)
	if err != nil {
		return err
	}
	s.db.mu.Lock()
	defer s.db.mu.Unlock()
	row, err := s.db.check(s.lease)
	if err != nil {
		return err
	}
	if s.revision != row.graphRevision {
		return fmt.Errorf("%w: thread runs %s, worker has %s", ErrRevisionMismatch, row.graphRevision, s.revision)
	}
	row.records = append(row.records, CheckpointRecord{
		Sequence: int64(len(row.records) + 1), Token: s.lease.Token, Worker: s.lease.Worker,
		GraphRevision: row.graphRevision, SchemaVersion: StateSchemaVersion, Codec: CodecRevision,
		StateHash: hash, LastNode: c.LastNode, Bytes: b,
	})
	return nil
}

func (s *fencedStore) Load(threadID string) (graph.Checkpoint, bool, error) {
	s.db.mu.Lock()
	row, ok := s.db.threads[threadID]
	var last *CheckpointRecord
	if ok && len(row.records) > 0 {
		r := row.records[len(row.records)-1]
		last = &r
	}
	revision := ""
	if ok {
		revision = row.graphRevision
	}
	s.db.mu.Unlock()
	if !ok {
		return graph.Checkpoint{}, false, ErrUnknownThread
	}
	if revision != s.revision {
		// SPEC 12.3: a new graph revision never continues from an old
		// LastNode; finish on the old revision or migrate to a new thread.
		return graph.Checkpoint{}, false, fmt.Errorf("%w: thread runs %s, worker has %s", ErrRevisionMismatch, revision, s.revision)
	}
	if last == nil {
		return graph.Checkpoint{}, false, nil
	}
	c, err := DecodeCheckpoint(last.Bytes)
	if err != nil {
		return graph.Checkpoint{}, false, err
	}
	if h, err := StateHash(c.State); err != nil || h != last.StateHash {
		return graph.Checkpoint{}, false, fmt.Errorf("checkpoint %d: state hash mismatch", last.Sequence)
	}
	return c, true, nil
}

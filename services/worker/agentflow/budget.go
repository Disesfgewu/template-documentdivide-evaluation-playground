package agentflow

import (
	"context"
	"errors"
	"fmt"
	"math"
	"sync"
)

var (
	ErrBudgetExceeded       = errors.New("budget exceeded")
	ErrCancelled            = errors.New("cancelled: no new paid call")
	ErrUnknownReservation   = errors.New("unknown reservation")
	ErrDuplicateReservation = errors.New("duplicate reservation")
	ErrLedgerOverflow       = errors.New("ledger total would overflow int64")
)

type Money struct {
	Micros          int64
	Currency        string
	PricingRevision string
}

type LedgerSnapshot struct {
	Limit    int64
	Spent    int64
	Reserved int64
	Unknown  int64
	Currency string
} // Unknown is the part of Reserved held by outcome_unknown reservations

type reservation struct {
	amount  int64
	unknown bool
}

type Ledger struct {
	mu              sync.Mutex
	limit           int64
	currency        string
	pricingRevision string
	spent           int64
	reservations    map[string]reservation
}

func NewLedger(limit Money) (*Ledger, error) {
	if limit.Micros <= 0 {
		return nil, fmt.Errorf("limit micros must be > 0: got %d", limit.Micros)
	}
	if limit.Currency == "" {
		return nil, errors.New("currency must not be empty")
	}
	if limit.PricingRevision == "" {
		return nil, errors.New("pricing revision must not be empty")
	}
	return &Ledger{
		limit:           limit.Micros,
		currency:        limit.Currency,
		pricingRevision: limit.PricingRevision,
		reservations:    make(map[string]reservation),
	}, nil
}

func (l *Ledger) Reserve(ctx context.Context, id string, amount Money) error {
	if err := ctx.Err(); err != nil {
		return ErrCancelled
	}
	l.mu.Lock()
	defer l.mu.Unlock()

	if err := ctx.Err(); err != nil {
		return ErrCancelled
	}
	if id == "" {
		return errors.New("reservation id must not be empty")
	}
	if amount.Currency != l.currency {
		return fmt.Errorf("currency mismatch: expected %q, got %q", l.currency, amount.Currency)
	}
	if amount.PricingRevision != l.pricingRevision {
		return fmt.Errorf("pricing revision mismatch: expected %q, got %q", l.pricingRevision, amount.PricingRevision)
	}
	if amount.Micros <= 0 {
		return fmt.Errorf("amount micros must be > 0: got %d", amount.Micros)
	}
	if _, exists := l.reservations[id]; exists {
		return ErrDuplicateReservation
	}

	// Remaining-budget subtraction instead of spent+reserved+amount, which
	// can wrap (PM-REVIEW on #39 at 4f904e7). All terms are >= 0 and the
	// reserved total never exceeds limit, so no step below can overflow.
	var reserved int64
	for _, r := range l.reservations {
		reserved += r.amount
	}
	remaining := int64(0)
	if l.spent <= l.limit && reserved <= l.limit-l.spent {
		remaining = l.limit - l.spent - reserved
	}
	if amount.Micros > remaining {
		return fmt.Errorf("%w: spent %d + reserved %d + requested %d > limit %d",
			ErrBudgetExceeded, l.spent, reserved, amount.Micros, l.limit)
	}

	l.reservations[id] = reservation{
		amount:  amount.Micros,
		unknown: false,
	}
	return nil
}

func (l *Ledger) Settle(id string, actual Money) error {
	l.mu.Lock()
	defer l.mu.Unlock()

	if actual.Currency != l.currency {
		return fmt.Errorf("currency mismatch: expected %q, got %q", l.currency, actual.Currency)
	}
	if actual.PricingRevision != l.pricingRevision {
		return fmt.Errorf("pricing revision mismatch: expected %q, got %q", l.pricingRevision, actual.PricingRevision)
	}
	if actual.Micros < 0 {
		return fmt.Errorf("actual micros must be >= 0: got %d", actual.Micros)
	}
	_, exists := l.reservations[id]
	if !exists {
		return ErrUnknownReservation
	}
	// Checked add: an actual cost may exceed its reservation (an honest
	// overrun), but spent must never wrap. On rejection the reservation
	// stays held and spent is unchanged.
	if actual.Micros > math.MaxInt64-l.spent {
		return fmt.Errorf("%w: spent %d + actual %d", ErrLedgerOverflow, l.spent, actual.Micros)
	}

	delete(l.reservations, id)
	l.spent += actual.Micros
	return nil
}

func (l *Ledger) Release(id string) error {
	l.mu.Lock()
	defer l.mu.Unlock()

	res, exists := l.reservations[id]
	if !exists {
		return ErrUnknownReservation
	}
	if res.unknown {
		return errors.New("cannot release outcome_unknown reservation")
	}

	delete(l.reservations, id)
	return nil
}

func (l *Ledger) MarkUnknown(id string) error {
	l.mu.Lock()
	defer l.mu.Unlock()

	res, exists := l.reservations[id]
	if !exists {
		return ErrUnknownReservation
	}
	res.unknown = true
	l.reservations[id] = res
	return nil
}

func (l *Ledger) Snapshot() LedgerSnapshot {
	l.mu.Lock()
	defer l.mu.Unlock()

	var reserved, unknown int64
	for _, r := range l.reservations {
		reserved += r.amount
		if r.unknown {
			unknown += r.amount
		}
	}
	return LedgerSnapshot{
		Limit:    l.limit,
		Spent:    l.spent,
		Reserved: reserved,
		Unknown:  unknown,
		Currency: l.currency,
	}
}

package agentflow

import (
	"context"
	"errors"
	"fmt"
	"math"
	"sync"
	"sync/atomic"
	"testing"
)

func TestBudgetSequentialReservationsAndRoomFreed(t *testing.T) {
	ctx := context.Background()
	m := Money{Micros: 1_000_000, Currency: "USD", PricingRevision: "2026-01"}
	l, err := NewLedger(m)
	if err != nil {
		t.Fatalf("unexpected NewLedger error: %v", err)
	}

	snap0 := l.Snapshot()
	if snap0 != (LedgerSnapshot{Limit: 1_000_000, Spent: 0, Reserved: 0, Unknown: 0, Currency: "USD"}) {
		t.Fatalf("unexpected initial snapshot: %+v", snap0)
	}

	amt300k := Money{Micros: 300_000, Currency: "USD", PricingRevision: "2026-01"}

	// 1st reservation of 300_000
	if err := l.Reserve(ctx, "r1", amt300k); err != nil {
		t.Fatalf("r1 reserve failed: %v", err)
	}
	snap1 := l.Snapshot()
	if snap1 != (LedgerSnapshot{Limit: 1_000_000, Spent: 0, Reserved: 300_000, Unknown: 0, Currency: "USD"}) {
		t.Fatalf("unexpected snapshot after r1: %+v", snap1)
	}

	// 2nd reservation of 300_000
	if err := l.Reserve(ctx, "r2", amt300k); err != nil {
		t.Fatalf("r2 reserve failed: %v", err)
	}
	snap2 := l.Snapshot()
	if snap2 != (LedgerSnapshot{Limit: 1_000_000, Spent: 0, Reserved: 600_000, Unknown: 0, Currency: "USD"}) {
		t.Fatalf("unexpected snapshot after r2: %+v", snap2)
	}

	// 3rd reservation of 300_000
	if err := l.Reserve(ctx, "r3", amt300k); err != nil {
		t.Fatalf("r3 reserve failed: %v", err)
	}
	snap3 := l.Snapshot()
	if snap3 != (LedgerSnapshot{Limit: 1_000_000, Spent: 0, Reserved: 900_000, Unknown: 0, Currency: "USD"}) {
		t.Fatalf("unexpected snapshot after r3: %+v", snap3)
	}

	// 4th reservation exceeds budget
	err4 := l.Reserve(ctx, "r4", amt300k)
	if !errors.Is(err4, ErrBudgetExceeded) {
		t.Fatalf("expected ErrBudgetExceeded for r4, got %v", err4)
	}
	snap4 := l.Snapshot()
	if snap4 != (LedgerSnapshot{Limit: 1_000_000, Spent: 0, Reserved: 900_000, Unknown: 0, Currency: "USD"}) {
		t.Fatalf("unexpected snapshot after rejected r4: %+v", snap4)
	}

	// Settle r1 with 250_000 frees room (spent 250_000, reserved 600_000)
	amt250k := Money{Micros: 250_000, Currency: "USD", PricingRevision: "2026-01"}
	if err := l.Settle("r1", amt250k); err != nil {
		t.Fatalf("settle r1 failed: %v", err)
	}
	snap5 := l.Snapshot()
	if snap5 != (LedgerSnapshot{Limit: 1_000_000, Spent: 250_000, Reserved: 600_000, Unknown: 0, Currency: "USD"}) {
		t.Fatalf("unexpected snapshot after settling r1: %+v", snap5)
	}

	// Settle r2 with 0 frees 300_000 room (spent 250_000, reserved 300_000)
	amt0 := Money{Micros: 0, Currency: "USD", PricingRevision: "2026-01"}
	if err := l.Settle("r2", amt0); err != nil {
		t.Fatalf("settle r2 failed: %v", err)
	}
	snap6 := l.Snapshot()
	if snap6 != (LedgerSnapshot{Limit: 1_000_000, Spent: 250_000, Reserved: 300_000, Unknown: 0, Currency: "USD"}) {
		t.Fatalf("unexpected snapshot after settling r2: %+v", snap6)
	}

	// So a new 300_000 reservation succeeds (spent 250_000, reserved 600_000)
	if err := l.Reserve(ctx, "r5", amt300k); err != nil {
		t.Fatalf("r5 reserve failed: %v", err)
	}
	snap7 := l.Snapshot()
	if snap7 != (LedgerSnapshot{Limit: 1_000_000, Spent: 250_000, Reserved: 600_000, Unknown: 0, Currency: "USD"}) {
		t.Fatalf("unexpected snapshot after r5: %+v", snap7)
	}
}

func TestBudgetSettlementFreesRoomForReservationExactLimit(t *testing.T) {
	ctx := context.Background()
	m := Money{Micros: 1_150_000, Currency: "USD", PricingRevision: "2026-01"}
	l, err := NewLedger(m)
	if err != nil {
		t.Fatalf("unexpected NewLedger error: %v", err)
	}
	amt300k := Money{Micros: 300_000, Currency: "USD", PricingRevision: "2026-01"}

	if err := l.Reserve(ctx, "r1", amt300k); err != nil {
		t.Fatalf("r1 reserve failed: %v", err)
	}
	if err := l.Reserve(ctx, "r2", amt300k); err != nil {
		t.Fatalf("r2 reserve failed: %v", err)
	}
	if err := l.Reserve(ctx, "r3", amt300k); err != nil {
		t.Fatalf("r3 reserve failed: %v", err)
	}

	err4 := l.Reserve(ctx, "r4", amt300k)
	if !errors.Is(err4, ErrBudgetExceeded) {
		t.Fatalf("expected ErrBudgetExceeded for r4, got %v", err4)
	}

	amt250k := Money{Micros: 250_000, Currency: "USD", PricingRevision: "2026-01"}
	if err := l.Settle("r1", amt250k); err != nil {
		t.Fatalf("settle r1 failed: %v", err)
	}

	snap := l.Snapshot()
	if snap != (LedgerSnapshot{Limit: 1_150_000, Spent: 250_000, Reserved: 600_000, Unknown: 0, Currency: "USD"}) {
		t.Fatalf("unexpected snapshot: %+v", snap)
	}

	if err := l.Reserve(ctx, "r5", amt300k); err != nil {
		t.Fatalf("r5 reserve failed: %v", err)
	}

	snapAfter := l.Snapshot()
	if snapAfter != (LedgerSnapshot{Limit: 1_150_000, Spent: 250_000, Reserved: 900_000, Unknown: 0, Currency: "USD"}) {
		t.Fatalf("unexpected snapshot after r5: %+v", snapAfter)
	}
}

func TestBudgetMarkUnknownBlocksBudgetAndReconciles(t *testing.T) {
	ctx := context.Background()
	l, err := NewLedger(Money{Micros: 1_000_000, Currency: "USD", PricingRevision: "2026-01"})
	if err != nil {
		t.Fatalf("unexpected NewLedger error: %v", err)
	}

	amt := Money{Micros: 500_000, Currency: "USD", PricingRevision: "2026-01"}
	if err := l.Reserve(ctx, "res1", amt); err != nil {
		t.Fatalf("Reserve failed: %v", err)
	}
	if err := l.MarkUnknown("res1"); err != nil {
		t.Fatalf("MarkUnknown failed: %v", err)
	}

	// MarkUnknown keeps it counted in Reserved and Unknown and blocks budget
	snap := l.Snapshot()
	if snap.Reserved != 500_000 || snap.Unknown != 500_000 || snap.Spent != 0 {
		t.Fatalf("unexpected snapshot: %+v", snap)
	}

	// Blocks budget for a call that would exceed limit
	errOver := l.Reserve(ctx, "res2", Money{Micros: 600_000, Currency: "USD", PricingRevision: "2026-01"})
	if !errors.Is(errOver, ErrBudgetExceeded) {
		t.Fatalf("expected ErrBudgetExceeded, got %v", errOver)
	}

	// Release of unknown returns error
	errRel := l.Release("res1")
	if errRel == nil || errRel.Error() != "cannot release outcome_unknown reservation" {
		t.Fatalf("expected 'cannot release outcome_unknown reservation', got %v", errRel)
	}

	// Settle of unknown reconciles
	actual := Money{Micros: 400_000, Currency: "USD", PricingRevision: "2026-01"}
	if err := l.Settle("res1", actual); err != nil {
		t.Fatalf("Settle of unknown failed: %v", err)
	}

	snapReconciled := l.Snapshot()
	if snapReconciled != (LedgerSnapshot{Limit: 1_000_000, Spent: 400_000, Reserved: 0, Unknown: 0, Currency: "USD"}) {
		t.Fatalf("unexpected reconciled snapshot: %+v", snapReconciled)
	}
}

func TestBudgetReleaseUnsent(t *testing.T) {
	ctx := context.Background()
	l, err := NewLedger(Money{Micros: 1_000_000, Currency: "USD", PricingRevision: "2026-01"})
	if err != nil {
		t.Fatalf("unexpected NewLedger error: %v", err)
	}
	amt := Money{Micros: 300_000, Currency: "USD", PricingRevision: "2026-01"}
	if err := l.Reserve(ctx, "r1", amt); err != nil {
		t.Fatalf("Reserve failed: %v", err)
	}
	if err := l.Release("r1"); err != nil {
		t.Fatalf("Release failed: %v", err)
	}
	snap := l.Snapshot()
	if snap.Spent != 0 || snap.Reserved != 0 {
		t.Fatalf("unexpected snapshot: %+v", snap)
	}
	// Releasing again returns ErrUnknownReservation
	if err := l.Release("r1"); !errors.Is(err, ErrUnknownReservation) {
		t.Fatalf("expected ErrUnknownReservation, got %v", err)
	}
}

func TestBudgetCancelledContext(t *testing.T) {
	l, err := NewLedger(Money{Micros: 1_000_000, Currency: "USD", PricingRevision: "2026-01"})
	if err != nil {
		t.Fatalf("unexpected NewLedger error: %v", err)
	}

	ctx, cancel := context.WithCancel(context.Background())
	cancel()

	amt := Money{Micros: 300_000, Currency: "USD", PricingRevision: "2026-01"}
	errRes := l.Reserve(ctx, "r1", amt)
	if !errors.Is(errRes, ErrCancelled) {
		t.Fatalf("expected ErrCancelled, got %v", errRes)
	}

	snap := l.Snapshot()
	if snap != (LedgerSnapshot{Limit: 1_000_000, Spent: 0, Reserved: 0, Unknown: 0, Currency: "USD"}) {
		t.Fatalf("snapshot should be unchanged, got %+v", snap)
	}
}

func TestBudgetDuplicateID(t *testing.T) {
	ctx := context.Background()
	l, err := NewLedger(Money{Micros: 1_000_000, Currency: "USD", PricingRevision: "2026-01"})
	if err != nil {
		t.Fatalf("unexpected NewLedger error: %v", err)
	}
	amt := Money{Micros: 300_000, Currency: "USD", PricingRevision: "2026-01"}
	if err := l.Reserve(ctx, "id1", amt); err != nil {
		t.Fatalf("first reserve failed: %v", err)
	}
	errDup := l.Reserve(ctx, "id1", amt)
	if !errors.Is(errDup, ErrDuplicateReservation) {
		t.Fatalf("expected ErrDuplicateReservation, got %v", errDup)
	}
}

func TestBudgetCurrencyAndRevisionMismatch(t *testing.T) {
	ctx := context.Background()
	l, err := NewLedger(Money{Micros: 1_000_000, Currency: "USD", PricingRevision: "2026-01"})
	if err != nil {
		t.Fatalf("unexpected NewLedger error: %v", err)
	}

	// Currency mismatch on Reserve
	errCurr := l.Reserve(ctx, "r1", Money{Micros: 100_000, Currency: "EUR", PricingRevision: "2026-01"})
	if errCurr == nil {
		t.Fatalf("expected error on currency mismatch in Reserve")
	}

	// Revision mismatch on Reserve
	errRev := l.Reserve(ctx, "r1", Money{Micros: 100_000, Currency: "USD", PricingRevision: "2025-12"})
	if errRev == nil {
		t.Fatalf("expected error on revision mismatch in Reserve")
	}

	// Reserve valid
	if err := l.Reserve(ctx, "r1", Money{Micros: 100_000, Currency: "USD", PricingRevision: "2026-01"}); err != nil {
		t.Fatalf("valid reserve failed: %v", err)
	}

	// Currency mismatch on Settle
	errSettleCurr := l.Settle("r1", Money{Micros: 100_000, Currency: "EUR", PricingRevision: "2026-01"})
	if errSettleCurr == nil {
		t.Fatalf("expected error on currency mismatch in Settle")
	}

	// Revision mismatch on Settle
	errSettleRev := l.Settle("r1", Money{Micros: 100_000, Currency: "USD", PricingRevision: "2025-12"})
	if errSettleRev == nil {
		t.Fatalf("expected error on revision mismatch in Settle")
	}
}

func TestBudgetInvalidLimit(t *testing.T) {
	if _, err := NewLedger(Money{Micros: 0, Currency: "USD", PricingRevision: "2026-01"}); err == nil {
		t.Fatalf("expected error for limit micros 0")
	}
	if _, err := NewLedger(Money{Micros: -500, Currency: "USD", PricingRevision: "2026-01"}); err == nil {
		t.Fatalf("expected error for negative limit micros")
	}
	if _, err := NewLedger(Money{Micros: 1_000_000, Currency: "", PricingRevision: "2026-01"}); err == nil {
		t.Fatalf("expected error for empty currency")
	}
	if _, err := NewLedger(Money{Micros: 1_000_000, Currency: "USD", PricingRevision: ""}); err == nil {
		t.Fatalf("expected error for empty pricing revision")
	}
}

// SPEC T28 "two workers race for the last budget"
func TestBudgetConcurrencyRaceLastBudgetT28(t *testing.T) {
	for iter := 0; iter < 20; iter++ {
		l, err := NewLedger(Money{Micros: 1_000_000, Currency: "USD", PricingRevision: "2026-01"})
		if err != nil {
			t.Fatalf("iter %d: NewLedger error: %v", iter, err)
		}

		start := make(chan struct{})
		var wg sync.WaitGroup
		var successCount, exceededCount int64

		for i := 0; i < 50; i++ {
			wg.Add(1)
			go func(idx int) {
				defer wg.Done()
				<-start
				id := fmt.Sprintf("goroutine-%d", idx)
				err := l.Reserve(context.Background(), id, Money{
					Micros:          300_000,
					Currency:        "USD",
					PricingRevision: "2026-01",
				})
				if err == nil {
					atomic.AddInt64(&successCount, 1)
				} else if errors.Is(err, ErrBudgetExceeded) {
					atomic.AddInt64(&exceededCount, 1)
				} else {
					t.Errorf("iter %d, worker %d: unexpected error: %v", iter, idx, err)
				}
			}(i)
		}

		close(start)
		wg.Wait()

		if successCount != 3 {
			t.Fatalf("iter %d: expected exactly 3 successes, got %d", iter, successCount)
		}
		if exceededCount != 47 {
			t.Fatalf("iter %d: expected exactly 47 budget exceeded, got %d", iter, exceededCount)
		}
		snap := l.Snapshot()
		if snap.Reserved != 900_000 {
			t.Fatalf("iter %d: expected Reserved == 900_000, got %d", iter, snap.Reserved)
		}
	}
}

// PM-REVIEW on #39 (4f904e7): with limit MaxInt64, reserving MaxInt64 and then
// 1 under another ID wrapped spent+reserved+amount to MinInt64 and passed the
// cap (R20/T28). Settlement could also wrap spent. Rejected mutations must
// leave the ledger unchanged.
func TestBudgetInt64BoundariesNeverWrap(t *testing.T) {
	ctx := context.Background()
	usd := func(n int64) Money { return Money{Micros: n, Currency: "USD", PricingRevision: "2026-01"} }
	same := func(t *testing.T, got, want LedgerSnapshot) {
		t.Helper()
		if got != want {
			t.Fatalf("ledger changed on a rejected mutation: got %+v want %+v", got, want)
		}
	}

	t.Run("reserve at MaxInt64 then 1 more is rejected", func(t *testing.T) {
		l, err := NewLedger(usd(math.MaxInt64))
		if err != nil {
			t.Fatal(err)
		}
		if err := l.Reserve(ctx, "a", usd(math.MaxInt64)); err != nil {
			t.Fatalf("reserve exactly the limit: %v", err)
		}
		before := l.Snapshot()
		if err := l.Reserve(ctx, "b", usd(1)); !errors.Is(err, ErrBudgetExceeded) {
			t.Fatalf("PM repro: want ErrBudgetExceeded, got %v", err)
		}
		if err := l.Reserve(ctx, "c", usd(math.MaxInt64)); !errors.Is(err, ErrBudgetExceeded) {
			t.Fatalf("second MaxInt64 reserve: want ErrBudgetExceeded, got %v", err)
		}
		same(t, l.Snapshot(), before)
	})

	t.Run("settle at the limit then reserve 1 is rejected", func(t *testing.T) {
		l, _ := NewLedger(usd(math.MaxInt64))
		_ = l.Reserve(ctx, "a", usd(10))
		if err := l.Settle("a", usd(math.MaxInt64)); err != nil {
			t.Fatalf("settle up to MaxInt64: %v", err)
		}
		if s := l.Snapshot(); s.Spent != math.MaxInt64 || s.Reserved != 0 {
			t.Fatalf("unexpected snapshot %+v", s)
		}
		if err := l.Reserve(ctx, "b", usd(1)); !errors.Is(err, ErrBudgetExceeded) {
			t.Fatalf("want ErrBudgetExceeded, got %v", err)
		}
	})

	t.Run("settle that would wrap spent is rejected and state kept", func(t *testing.T) {
		l, _ := NewLedger(usd(math.MaxInt64))
		_ = l.Reserve(ctx, "a", usd(5))
		_ = l.Reserve(ctx, "b", usd(5))
		if err := l.Settle("a", usd(math.MaxInt64-1)); err != nil {
			t.Fatal(err)
		}
		before := l.Snapshot()
		if err := l.Settle("b", usd(2)); !errors.Is(err, ErrLedgerOverflow) {
			t.Fatalf("want ErrLedgerOverflow, got %v", err)
		}
		same(t, l.Snapshot(), before)
		// The reservation is still held and settles at the exact boundary.
		if err := l.Settle("b", usd(1)); err != nil {
			t.Fatalf("settle to exactly MaxInt64: %v", err)
		}
		if s := l.Snapshot(); s.Spent != math.MaxInt64 || s.Reserved != 0 {
			t.Fatalf("unexpected snapshot %+v", s)
		}
	})

	t.Run("overrun beyond the limit blocks new reservations", func(t *testing.T) {
		l, _ := NewLedger(usd(100))
		_ = l.Reserve(ctx, "a", usd(10))
		_ = l.Reserve(ctx, "b", usd(10))
		if err := l.Settle("a", usd(150)); err != nil {
			t.Fatal(err)
		}
		before := l.Snapshot()
		if err := l.Reserve(ctx, "c", usd(1)); !errors.Is(err, ErrBudgetExceeded) {
			t.Fatalf("want ErrBudgetExceeded after an overrun, got %v", err)
		}
		same(t, l.Snapshot(), before)
	})

	t.Run("exact remaining is accepted, one more is not", func(t *testing.T) {
		l, _ := NewLedger(usd(math.MaxInt64))
		_ = l.Reserve(ctx, "a", usd(math.MaxInt64-7))
		if err := l.Reserve(ctx, "b", usd(8)); !errors.Is(err, ErrBudgetExceeded) {
			t.Fatalf("want ErrBudgetExceeded, got %v", err)
		}
		if err := l.Reserve(ctx, "b", usd(7)); err != nil {
			t.Fatalf("exact remaining: %v", err)
		}
		if s := l.Snapshot(); s.Reserved != math.MaxInt64 {
			t.Fatalf("reserved %d", s.Reserved)
		}
	})
}

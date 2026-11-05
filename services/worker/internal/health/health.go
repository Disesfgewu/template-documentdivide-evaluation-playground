// Package health serves the worker liveness endpoint.
package health

import (
	"encoding/json"
	"net/http"
)

// Handler returns the worker HTTP handler. /healthz only reports that the
// process is alive; it does not check dependencies.
func Handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]string{"status": "ok", "service": "lab-worker"})
	})
	return mux
}

// Generates Grafana dashboards into helm/iep/dashboards/.
// Run: go -C tools/dashboards run . ../../helm/iep/dashboards
// The JSON is committed; nothing generates it at deploy time.
//
// Ignore the DashboardBuilder deprecation: v2 is a Kubernetes resource
// the file provisioner rejects. v1 stays until the delivery path changes.
package main

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"

	"github.com/grafana/grafana-foundation-sdk/go/dashboard"
)

func main() {
	outDir := "../../helm/iep/dashboards"
	if len(os.Args) > 1 {
		outDir = os.Args[1]
	}
	write(outDir, "iep-overview.json", appDashboard())
	write(outDir, "iep-redis.json", redisDashboard())
}

func appDashboard() *dashboard.DashboardBuilder {
	return dashboard.NewDashboardBuilder("IEP - application").
		Uid("iep-overview").
		Tags([]string{"iep"}).
		Refresh("30s").
		Time("now-6h", "now").
		Timezone("browser").

		// round() + decimals 0: increase() extrapolates, so 1 event can read 1.03.
		WithPanel(panel(panelOpts{
			title:    "Vote outcomes",
			desc:     "How votes ended. timeout means the vote deadline passed with no Finalized event: the watcher polls an event filter inside a bare except, so a dropped web3 connection is swallowed and the order stays pending. filter_error means create_filter raised and the watcher never started at all.",
			unit:     "short",
			decimals: decimals(0),
			stacked:  true,
			targets: []target{{
				expr:   "round(sum by (outcome) (increase(iep_voting_outcome_total[5m])))",
				legend: "{{outcome}}",
			}},
		})).

		WithPanel(panel(panelOpts{
			title:    "Vote watcher threads in flight",
			desc:     "One thread per in-flight vote, all in ONE gunicorn worker. This is the constraint that pins director to replicaCount 1. Adding --workers would break every gauge on this dashboard.",
			unit:     "short",
			decimals: decimals(0),
			targets:  []target{{expr: "iep_voting_threads_active", legend: "{{pod}}"}},
		})).

		WithPanel(panel(panelOpts{
			title: "Vote duration",
			desc:  "Deploy to Finalized. Buckets top out at 3600s because that is VOTING_DEADLINE_SECONDS. Nothing in the HTTP metrics can measure this.",
			unit:  "s",
			targets: []target{
				{expr: "histogram_quantile(0.95, sum by (le) (rate(iep_voting_duration_seconds_bucket[30m])))", legend: "p95"},
				{expr: "histogram_quantile(0.50, sum by (le) (rate(iep_voting_duration_seconds_bucket[30m])))", legend: "p50"},
			},
		})).

		WithPanel(panel(panelOpts{
			title:    "Pending orders: app view vs Redis view",
			desc:     "director counts keys matching the pending_order prefix. The exporter counts every key in db0. A persistent gap means something writes to Redis that director does not know about.",
			unit:     "short",
			decimals: decimals(0),
			targets: []target{
				{expr: "iep_pending_orders", legend: "director (pending_order prefix)"},
				{expr: "redis_db_keys{db=\"db0\"}", legend: "exporter (all keys in db0)"},
			},
		})).

		WithPanel(panel(panelOpts{
			title:   "Database calls per second",
			desc:    "Which service drives which backend. Counts WIRE commands, not logical calls -- count_documents() sends aggregate, and scan_iter pages with several SCANs.",
			unit:    "ops",
			stacked: true,
			targets: []target{{
				expr:   "sum by (job, backend) (rate(iep_db_operations_total[5m]))",
				legend: "{{job}} -> {{backend}}",
			}},
		})).
		WithPanel(panel(panelOpts{
			title: "Database latency p95",
			desc:  "Client-side wall time, so it includes network and serialisation. Server-side timing would read lower.",
			unit:  "s",
			targets: []target{{
				expr:   "histogram_quantile(0.95, sum by (le, backend, operation) (rate(iep_db_operation_duration_seconds_bucket[5m])))",
				legend: "{{backend}} {{operation}}",
			}},
		})).
		WithPanel(panel(panelOpts{
			title:    "Login outcomes",
			desc:     "unknown-user and wrong-password share the invalid_credentials label on purpose: splitting them would leak which addresses are registered.",
			unit:     "short",
			decimals: decimals(0),
			stacked:  true,
			targets: []target{{
				expr:   "round(sum by (result) (increase(iep_login_total[15m])))",
				legend: "{{result}}",
			}},
		})).

		WithPanel(panel(panelOpts{
			title: "HTTP requests per second",
			desc:  "nginx counts what arrived at the edge; Flask counts what each service served. The difference is traffic nginx rejected or served itself.",
			unit:  "reqps",
			targets: []target{
				{expr: "sum by (job) (rate(flask_http_request_total[5m]))", legend: "flask: {{job}}"},
				{expr: "sum (rate(nginx_ingress_controller_requests[5m]))", legend: "nginx edge"},
			},
		}))
}

func redisDashboard() *dashboard.DashboardBuilder {
	return dashboard.NewDashboardBuilder("IEP - Redis").
		Uid("iep-redis-sdk").
		Tags([]string{"iep"}).
		Refresh("30s").
		Time("now-6h", "now").
		Timezone("browser").

		WithPanel(panel(panelOpts{
			title: "Memory used against the cap",
			desc:  "used_memory is the allocator's accounting, not process RSS. maxmemory is enforced against THIS number, so this is the pair that decides when writes start failing.",
			unit:  "bytes",
			targets: []target{
				{expr: "redis_memory_used_bytes", legend: "used"},
				{expr: "redis_memory_max_bytes", legend: "maxmemory"},
			},
		})).
		WithPanel(panel(panelOpts{
			title: "Memory as a fraction of the cap",
			desc:  "The IepRedisMemoryHigh alert fires above 0.8. Policy is noeviction, so at 1.0 writes are refused with an error rather than keys being dropped.",
			unit:  "percentunit",
			targets: []target{{
				expr:   "redis_memory_used_bytes / redis_memory_max_bytes",
				legend: "used / maxmemory",
			}},
		})).
		WithPanel(panel(panelOpts{
			title:    "Keys per database",
			desc:     "Redis has 16 databases by default; only db0 is used here. db0 should track iep_pending_orders on the application dashboard.",
			unit:     "short",
			decimals: decimals(0),
			targets:  []target{{expr: "redis_db_keys", legend: "{{db}}"}},
		})).
		WithPanel(panel(panelOpts{
			title:    "Evicted and expired keys",
			desc:     "Evicted MUST stay flat at zero. maxmemory-policy is noeviction, so any eviction at all means the policy is not what this chart thinks it is -- and a dropped key is a lost order.",
			unit:     "short",
			decimals: decimals(0),
			targets: []target{
				{expr: "rate(redis_evicted_keys_total[5m])", legend: "evicted/s"},
				{expr: "rate(redis_expired_keys_total[5m])", legend: "expired/s"},
			},
		})).
		WithPanel(panel(panelOpts{
			title:   "Commands per second",
			desc:    "By command name. Includes the exporter's own INFO and director's scrape-time SCAN, so this never reaches zero.",
			unit:    "ops",
			stacked: true,
			targets: []target{{
				expr:   "sum by (cmd) (rate(redis_commands_total[5m]))",
				legend: "{{cmd}}",
			}},
		})).
		WithPanel(panel(panelOpts{
			title: "Command latency, server side",
			desc:  "Total command time divided by command count. Compare against iep_db_operation_duration_seconds on the application dashboard: the gap is network and client library time.",
			unit:  "s",
			targets: []target{{
				expr:   "rate(redis_commands_duration_seconds_total[5m]) / rate(redis_commands_total[5m])",
				legend: "{{cmd}}",
			}},
		})).
		WithPanel(panel(panelOpts{
			title:    "Clients",
			desc:     "blocked_clients above zero means someone is using a blocking command such as BLPOP. This codebase uses none, so it should stay flat.",
			unit:     "short",
			decimals: decimals(0),
			targets: []target{
				{expr: "redis_connected_clients", legend: "connected"},
				{expr: "redis_blocked_clients", legend: "blocked"},
			},
		})).
		WithPanel(panel(panelOpts{
			title: "Network throughput",
			desc:  "Useful mainly as a sanity check that traffic matches the command rate above.",
			unit:  "Bps",
			targets: []target{
				{expr: "rate(redis_net_input_bytes_total[5m])", legend: "in"},
				{expr: "rate(redis_net_output_bytes_total[5m])", legend: "out"},
			},
		}))
}

func write(dir, name string, b *dashboard.DashboardBuilder) {
	d, err := b.Build()
	if err != nil {
		panic(err)
	}
	out, err := json.MarshalIndent(d, "", "  ")
	if err != nil {
		panic(err)
	}
	path := filepath.Join(dir, name)
	if err := os.WriteFile(path, append(out, '\n'), 0o644); err != nil {
		panic(err)
	}
	fmt.Printf("  %-24s %d panels, %d bytes\n", name, len(d.Panels), len(out))
}

package main

import (
	"encoding/json"
	"github.com/router-for-me/CLIProxyAPI/v8/sdk/pluginabi"
	"github.com/router-for-me/CLIProxyAPI/v8/sdk/pluginapi"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func flag(v bool) *bool { return &v }
func TestRewrite(t *testing.T) {
	for _, value := range []string{"fast", "priority", "ultrafast", "default", "", " Fast ", "future-tier"} {
		for _, allow := range []bool{false, true} {
			for _, supports := range []bool{false, true} {
				for _, mapping := range []bool{false, true} {
					raw := []byte(`{"model":"x","input":[],"other":9007199254740993}`)
					if value != "" {
						raw = []byte(`{"service_tier":` + string(mustJSON(value)) + `,"other":9007199254740993}`)
					}
					out, err := rewrite(raw, accountPolicy{AllowFast: flag(allow), SupportsUltrafast: supports}, mapping)
					if err != nil {
						t.Fatal(err)
					}
					expected := any(value)
					if value == "" {
						expected = "absent"
					}
					if !allow && (value == "fast" || value == "priority" || value == "ultrafast" || value == " Fast ") {
						expected = "absent"
					}
					if allow && value == "ultrafast" && mapping && !supports {
						expected = "priority"
					}
					var obj map[string]json.RawMessage
					_ = json.Unmarshal(out, &obj)
					actual := any("absent")
					if raw, found := obj["service_tier"]; found {
						if err := json.Unmarshal(raw, &actual); err != nil {
							t.Fatal(err)
						}
					}
					if actual != expected {
						t.Fatalf("value=%q allow=%v support=%v mapping=%v: %s", value, allow, supports, mapping, out)
					}
					if string(obj["other"]) != "9007199254740993" {
						t.Fatalf("unrelated number changed: %s", out)
					}
				}
			}
		}
	}
}
func mustJSON(v any) []byte { b, _ := json.Marshal(v); return b }
func TestPolicyReload(t *testing.T) {
	path := filepath.Join(t.TempDir(), "policy.json")
	state.Lock()
	state.cfg = pluginConfig{PolicyPath: path}
	state.info = nil
	state.Unlock()
	meta := map[string]any{"selected_auth_id": "nested/account.json", "selected_auth_index": "idx"}
	check := func(want bool) {
		t.Helper()
		p, _ := lookupPolicy(meta)
		got := p.AllowFast == nil || *p.AllowFast
		if got != want {
			t.Fatalf("allow=%v want=%v", got, want)
		}
	}
	check(true)
	if err := os.WriteFile(path, []byte(`{"files":{"account.json":{"allow_fast":false}}}`), 0600); err != nil {
		t.Fatal(err)
	}
	check(false)
	if os.Geteuid() != 0 {
		if err := os.Chmod(path, 0); err != nil {
			t.Fatal(err)
		}
		check(true)
		if err := os.Chmod(path, 0600); err != nil {
			t.Fatal(err)
		}
		check(false)
	}
	next := path + ".tmp"
	_ = os.WriteFile(next, []byte(`{"accounts":{"idx":{"allow_fast":true}},"files":{"account.json":{"allow_fast":false}}}`), 0600)
	_ = os.Rename(next, path)
	check(true)
	_ = os.WriteFile(path, []byte(`broken`), 0600)
	check(true)
	_ = os.Remove(path)
	check(true)
}
func TestInvalidAndMissingTier(t *testing.T) {
	for _, raw := range []string{`{"service_tier":null}`, `{"service_tier":3}`, `{"input":[]}`} {
		out, err := rewrite([]byte(raw), accountPolicy{}, true)
		if err != nil || string(out) != raw {
			t.Fatalf("changed %s", raw)
		}
	}
	if _, err := rewrite([]byte(`broken`), accountPolicy{}, false); err == nil {
		t.Fatal("invalid JSON accepted")
	}
}
func BenchmarkPolicyUnchanged(b *testing.B) {
	path := filepath.Join(b.TempDir(), "policy.json")
	_ = os.WriteFile(path, []byte(`{"accounts":{"idx":{"allow_fast":false}}}`), 0600)
	state.Lock()
	state.cfg = pluginConfig{PolicyPath: path}
	state.info = nil
	state.Unlock()
	raw := []byte(`{"service_tier":"priority","input":[],"model":"gpt-5.5"}`)
	meta := map[string]any{"selected_auth_index": "idx"}
	b.ResetTimer()
	for b.Loop() {
		p, cfg := lookupPolicy(meta)
		_, _ = rewrite(raw, p, cfg.MapUnsupportedUltrafast)
	}
}

func TestStatusLifecycle(t *testing.T) {
	dir := t.TempDir()
	cfg := pluginConfig{PolicyPath: filepath.Join(dir, "policy.json"), StatusPath: filepath.Join(dir, "status.json"), MapUnsupportedUltrafast: true}
	register := func(config pluginConfig) error {
		raw := mustJSON(map[string]any{"config_yaml": []byte("policy_path: " + config.PolicyPath + "\nstatus_path: " + config.StatusPath + "\nmap_unsupported_ultrafast: true\n")})
		_, err := dispatch(pluginabi.MethodPluginRegister, raw)
		return err
	}
	if err := register(cfg); err != nil {
		t.Fatal(err)
	}
	raw, err := os.ReadFile(cfg.StatusPath)
	if err != nil {
		t.Fatal(err)
	}
	var first pluginStatus
	if err := json.Unmarshal(raw, &first); err != nil {
		t.Fatal(err)
	}
	if first.Plugin != "fast-mode-policy" || first.Version != version || first.PID != os.Getpid() || !first.MapUnsupportedUltrafast {
		t.Fatalf("status: %+v", first)
	}
	if _, err := time.Parse(time.RFC3339, first.LoadedAt); err != nil {
		t.Fatal(err)
	}
	info, _ := os.Stat(cfg.StatusPath)
	if info.Mode().Perm() != 0640 {
		t.Fatal("wrong status mode")
	}
	if err := register(cfg); err != nil {
		t.Fatal(err)
	}
	if err := removeOwnStatus(cfg.StatusPath, first.LoadedAt); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(cfg.StatusPath); err != nil {
		t.Fatal("old instance removed new status", err)
	}
	_, err = dispatch(pluginabi.MethodPluginReconfigure, mustJSON(map[string]any{"config_yaml": []byte("policy_path: [bad]\n")}))
	if err == nil {
		t.Fatal("bad configuration accepted")
	}
	if _, err := os.Stat(cfg.StatusPath); !os.IsNotExist(err) {
		t.Fatal("failed reconfiguration left stale liveness status")
	}
	if err := register(cfg); err != nil {
		t.Fatal(err)
	}
	cliproxyPluginShutdown()
	if _, err := os.Stat(cfg.StatusPath); !os.IsNotExist(err) {
		t.Fatal("status not removed")
	}
	cfg.StatusPath = filepath.Join(dir, "missing", "status.json")
	if err := register(cfg); err == nil {
		t.Fatal("status publication failure must reject registration")
	}
}

func TestInterceptorFailOpen(t *testing.T) {
	for _, raw := range [][]byte{[]byte(`broken`), mustJSON(pluginapi.RequestInterceptRequest{ToFormat: "codex", Body: []byte(`broken`)})} {
		result, err := dispatch(pluginabi.MethodRequestInterceptAfter, raw)
		if err != nil {
			t.Fatal(err)
		}
		response, ok := result.(pluginapi.RequestInterceptResponse)
		if !ok || response.Body != nil {
			t.Fatalf("must leave the host body unchanged: %+v", result)
		}
	}
}

func TestPolicyDefaultsAndIndexNoMerge(t *testing.T) {
	path := filepath.Join(t.TempDir(), "policy.json")
	state.cfg = pluginConfig{PolicyPath: path}
	state.info = nil
	_ = os.WriteFile(path, []byte(`{"version":1,"accounts":{"idx":{}},"files":{"account.json":{"allow_fast":false,"supports_ultrafast":true}}}`), 0600)
	p, _ := lookupPolicy(map[string]any{"selected_auth_index": "idx", "selected_auth_id": "account.json"})
	if p.AllowFast != nil || p.SupportsUltrafast {
		t.Fatal("index entry must not merge filename fields")
	}
	raw := []byte(`{"service_tier":"ultrafast"}`)
	out, err := rewrite(raw, p, true)
	if err != nil || tier(out) != "priority" {
		t.Fatal("unknown supports defaults false")
	}
	for _, policy := range []string{`null`, `[]`, `{"files":{"account.json":{"allow_fast":"wrong"}}}`} {
		_ = os.WriteFile(path, []byte(policy), 0600)
		state.info = nil
		p, _ = lookupPolicy(map[string]any{"selected_auth_id": "account.json"})
		if p.AllowFast != nil || p.SupportsUltrafast {
			t.Fatal("malformed policy did not default")
		}
	}
}

func TestWebsocketWarningDeduplication(t *testing.T) {
	state.warned = make(map[string]bool)
	state.lastWarning = time.Time{}
	warnWebsocket(nil, false, "http")
	if len(state.warned) != 0 {
		t.Fatal("HTTP request warned")
	}
	warnWebsocket(map[string]any{"execution_session_id": "session-one"}, false, "")
	warnWebsocket(map[string]any{"execution_session_id": "session-one"}, false, "")
	if len(state.warned) != 1 {
		t.Fatal("session warning was not deduplicated")
	}
	warnWebsocket(map[string]any{"execution_session_id": "session-two"}, false, "")
	if len(state.warned) != 1 {
		t.Fatal("global warning rate limit missing")
	}
	state.lastWarning = time.Time{}
	warnWebsocket(map[string]any{"execution_session_id": "session-two"}, false, "")
	if len(state.warned) != 2 {
		t.Fatal("second session never warned")
	}
}

func TestRecoveredPanicLeavesBodyAndLockUsable(t *testing.T) {
	// Deliberately break warning bookkeeping to force an internal Go panic.
	state.warned = nil
	state.lastWarning = time.Time{}
	raw := mustJSON(pluginapi.RequestInterceptRequest{ToFormat: "codex", Body: []byte(`{"service_tier":"priority"}`), Headers: http.Header{"Upgrade": []string{"websocket"}}, RequestID: "panic-session"})
	result, err := dispatch(pluginabi.MethodRequestInterceptAfter, raw)
	if err != nil {
		t.Fatal(err)
	}
	if result.(pluginapi.RequestInterceptResponse).Body != nil {
		t.Fatal("panic changed the request")
	}
	if !state.TryLock() {
		t.Fatal("panic left the policy mutex locked")
	}
	state.warned = make(map[string]bool)
	state.Unlock()
	if rc := cliproxy_plugin_init(nil, nil); rc != 1 {
		t.Fatal("nil ABI initialization accepted")
	}
	cliproxyPluginFree(nil, 0)
}

func TestConfigDefaultsUseSharedReadableGatewayState(t *testing.T) {
	cfg, err := decodeConfig(nil)
	if err != nil {
		t.Fatal(err)
	}
	if cfg.StatusPath != "/var/lib/cliproxyapi/fast-mode-plugin-status.json" || cfg.PolicyPath != "/var/lib/personal-codex-router/fast-mode-policy.json" {
		t.Fatalf("wrong shared policy/status defaults: %+v", cfg)
	}
	if filepath.Dir(cfg.StatusPath) != "/var/lib/cliproxyapi" {
		t.Fatal("status must be directly under gateway StateDirectory, outside auth/")
	}
	custom := filepath.Join(t.TempDir(), "status.json")
	cfg, err = decodeConfig([]byte("status_path: " + custom + "\n"))
	if err != nil || cfg.StatusPath != custom || cfg.PolicyPath != "/var/lib/personal-codex-router/fast-mode-policy.json" {
		t.Fatalf("override did not preserve policy default: %+v, %v", cfg, err)
	}
}

func TestAuditTierClosedVocabulary(t *testing.T) {
	for _, label := range []string{"normal", "priority", "fast", "ultrafast", "default", "auto", "absent"} {
		if got := tier(mustJSON(map[string]any{"service_tier": " " + strings.ToUpper(label) + " "})); got != label {
			t.Fatalf("tier label %q: %q", label, got)
		}
	}
	for _, test := range []struct{ body, want string }{
		{`{}`, "absent"}, {`null`, "invalid"}, {`[]`, "invalid"}, {`broken`, "invalid"},
		{`{"service_tier":"Bearer TEST-SECRET"}`, "invalid"},
		{`{"service_tier":{"access_token":"TEST-SECRET"}}`, "non-string"},
		{`{"service_tier":["TEST-SECRET"]}`, "non-string"},
		{`{"service_tier":null}`, "non-string"}, {`{"service_tier":3}`, "non-string"},
		{`{"service_tier":true}`, "non-string"}, {`{"service_tier":"fast\nTEST-SECRET"}`, "invalid"},
	} {
		if got := tier([]byte(test.body)); got != test.want {
			t.Fatalf("tier %s: %q, want %q", test.body, got, test.want)
		}
	}
}

func TestAuditAuthIndexValidation(t *testing.T) {
	for _, value := range []any{"Bearer TEST-SECRET", strings.Repeat("a", 10000), "0123456789abcde\n", "../auth.json", map[string]any{"token": "TEST-SECRET"}, 123} {
		if got := auditAuthIndex(value); got != "invalid" {
			t.Fatalf("unsafe index: %q", got)
		}
	}
	if auditAuthIndex(nil) != "absent" || auditAuthIndex("0123456789ABCDEF") != "0123456789abcdef" {
		t.Fatal("index normalization failed")
	}
}

func TestAllPluginLogsExcludeUntrustedData(t *testing.T) {
	original := hostLog
	t.Cleanup(func() { hostLog = original })
	var logs []string
	hostLog = func(level, message string, fields map[string]any) {
		logs = append(logs, string(mustJSON(map[string]any{"level": level, "message": message, "fields": fields})))
	}
	dir := t.TempDir()
	cfg := pluginConfig{PolicyPath: filepath.Join(dir, "TEST-SECRET-policy.json"), StatusPath: filepath.Join(dir, "status.json"), Audit: true}
	if err := configure(cfg); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(cfg.PolicyPath, []byte(`{"TEST-SECRET":"malformed-policy"`), 0600); err != nil {
		t.Fatal(err)
	}
	for _, value := range []any{"Bearer TEST-SECRET", map[string]any{"token": "TEST-SECRET"}, []any{"TEST-SECRET"}, nil, 42, "priority"} {
		state.lastWarning = time.Time{}
		lastError = time.Time{}
		req := pluginapi.RequestInterceptRequest{
			ToFormat: "codex", Body: mustJSON(map[string]any{"service_tier": value, "input": "TEST-SECRET"}),
			Headers: http.Header{"Upgrade": []string{"websocket"}, "Authorization": []string{"Bearer TEST-SECRET"}},
			Metadata: map[string]any{"selected_auth_id": "/TEST-SECRET/credential.json", "selected_auth_index": value,
				"execution_session_id": "TEST-SECRET-session", "canonical_session_id": "TEST-SECRET-canonical"},
			RequestID: "TEST-SECRET-request",
		}
		if _, err := dispatch(pluginabi.MethodRequestInterceptAfter, mustJSON(req)); err != nil {
			t.Fatal(err)
		}
	}
	// Malformed request/config errors may contain offending data internally.
	for _, method := range []string{pluginabi.MethodRequestInterceptAfter, pluginabi.MethodPluginReconfigure, "TEST-SECRET-method"} {
		lastError = time.Time{}
		_, _ = dispatch(method, []byte(`{"TEST-SECRET":`))
	}
	for _, raw := range logs {
		if strings.Contains(raw, "TEST-SECRET") || strings.Contains(raw, "credential.json") || strings.Contains(raw, "auth_id") || strings.Contains(raw, `"session"`) {
			t.Fatalf("untrusted data in plugin log: %s", raw)
		}
	}
	if len(logs) < 9 {
		t.Fatalf("expected decision, warning, policy and operation error logs: %v", logs)
	}
}

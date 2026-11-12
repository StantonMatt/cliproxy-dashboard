package main

/*
#include <stdint.h>
#include <stdlib.h>

typedef struct {
	void* ptr;
	size_t len;
} cliproxy_buffer;

typedef struct {
	uint32_t abi_version;
	void* host_ctx;
	void* call;
	void* free_buffer;
} cliproxy_host_api;
static int host_call(cliproxy_host_api* h, char* m, void* req, size_t n, cliproxy_buffer* out) {
 return ((int (*)(void*, char*, void*, size_t, cliproxy_buffer*))h->call)(h->host_ctx, m, req, n, out);
}
static void host_free(cliproxy_host_api* h, cliproxy_buffer* b) {
 if(b->ptr) ((void (*)(void*, size_t))h->free_buffer)(b->ptr, b->len);
}

typedef int (*cliproxy_plugin_call_fn)(char*, uint8_t*, size_t, cliproxy_buffer*);
typedef void (*cliproxy_plugin_free_fn)(void*, size_t);
typedef void (*cliproxy_plugin_shutdown_fn)(void);

typedef struct {
	uint32_t abi_version;
	cliproxy_plugin_call_fn call;
	cliproxy_plugin_free_fn free_buffer;
	cliproxy_plugin_shutdown_fn shutdown;
} cliproxy_plugin_api;

extern int cliproxyPluginCall(char*, uint8_t*, size_t, cliproxy_buffer*);
extern void cliproxyPluginFree(void*, size_t);
extern void cliproxyPluginShutdown(void);
*/
import "C"

import (
	"bytes"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"
	"unsafe"

	"github.com/router-for-me/CLIProxyAPI/v8/sdk/pluginabi"
	"github.com/router-for-me/CLIProxyAPI/v8/sdk/pluginapi"
	"github.com/tidwall/sjson"
	"gopkg.in/yaml.v3"
)

// The ABI is in-process; recover here, since a panic cannot cross the C boundary safely.
const version = "0.2.0"

// policySchema is the newest policy file schema this library enforces. The
// dashboard offers the ultrafast level only when a live status reports 2.
const policySchema = 2

var hostAPI *C.cliproxy_host_api
var state = struct {
	sync.Mutex
	cfg         pluginConfig
	info        os.FileInfo
	policy      policyFile
	warned      map[string]bool
	lastWarning time.Time
	loadedAt    string
}{warned: make(map[string]bool)}

type pluginConfig struct {
	StatusPath              string `yaml:"status_path"`
	PolicyPath              string `yaml:"policy_path"`
	MapUnsupportedUltrafast bool   `yaml:"map_unsupported_ultrafast"`
	Audit                   bool   `yaml:"audit"` // Validated auth index and tier labels only.
}

// Schema 2 adds level and ultrafast_models. A valid level wins; otherwise the
// v1 fields apply exactly as in 0.1.x. The dashboard also writes the v1 fields
// so an older library still enforces a sensible policy after a rollback.
type accountPolicy struct {
	AllowFast         *bool    `json:"allow_fast"`
	SupportsUltrafast bool     `json:"supports_ultrafast"`
	Level             string   `json:"level"`
	UltrafastModels   []string `json:"ultrafast_models"`
}

func (p *accountPolicy) UnmarshalJSON(raw []byte) error {
	type plain accountPolicy
	var next plain
	if err := json.Unmarshal(raw, &next); err != nil {
		return err
	}
	// Go accepts null for strings and string-slice elements as zero values.
	// Reject those explicitly, while absent fields retain schema 1 behavior.
	var fields struct {
		Level           json.RawMessage   `json:"level"`
		UltrafastModels []json.RawMessage `json:"ultrafast_models"`
	}
	if err := json.Unmarshal(raw, &fields); err != nil {
		return err
	}
	if bytes.Equal(bytes.TrimSpace(fields.Level), []byte("null")) {
		return fmt.Errorf("policy level must be a string")
	}
	for _, model := range fields.UltrafastModels {
		if bytes.Equal(bytes.TrimSpace(model), []byte("null")) {
			return fmt.Errorf("policy ultrafast model must be a string")
		}
	}
	*p = accountPolicy(next)
	return nil
}

// level returns normal, fast or ultrafast for a schema 2 entry, or "" when the
// entry has no valid level and the v1 rules apply.
func (p accountPolicy) level() string {
	switch level := strings.ToLower(strings.TrimSpace(p.Level)); level {
	case "normal", "fast", "ultrafast":
		return level
	}
	return ""
}

// allowsUltrafast reports whether the normalized model is on this account's
// ultrafast allowlist. Unknown or empty models are never allowed.
func (p accountPolicy) allowsUltrafast(model string) bool {
	if model == "" {
		return false
	}
	for _, allowed := range p.UltrafastModels {
		if normalizeModel(allowed) == model {
			return true
		}
	}
	return false
}

// normalizeModel lowercases a model name and strips a trailing thinking suffix
// such as "(high)" and any "prefix/" so it matches catalog slugs exactly.
func normalizeModel(model string) string {
	model = strings.ToLower(strings.TrimSpace(model))
	if strings.HasSuffix(model, ")") {
		if i := strings.LastIndex(model, "("); i >= 0 {
			model = strings.TrimSpace(model[:i])
		}
	}
	if i := strings.LastIndex(model, "/"); i >= 0 {
		model = model[i+1:]
	}
	return strings.TrimSpace(model)
}

// Only these client formats' Codex translators forward "ultrafast" upstream;
// the Claude, Gemini and Interactions translators drop it.
func passesUltrafast(sourceFormat string) bool {
	switch strings.ToLower(strings.TrimSpace(sourceFormat)) {
	case "openai-response", "openai":
		return true
	}
	return false
}

func requestModel(model string, body []byte) string {
	if strings.TrimSpace(model) == "" {
		var obj struct {
			Model any `json:"model"`
		}
		if json.Unmarshal(body, &obj) == nil {
			model, _ = obj.Model.(string)
		}
	}
	return normalizeModel(model)
}

type policyFile struct {
	Accounts map[string]accountPolicy `json:"accounts"` // keyed by gateway auth_index
	Files    map[string]accountPolicy `json:"files"`    // keyed by auth file basename; index takes precedence
}
type envelope struct {
	OK     bool `json:"ok"`
	Result any  `json:"result,omitempty"`
	Error  any  `json:"error,omitempty"`
}

func main() {}

//export cliproxy_plugin_init
func cliproxy_plugin_init(host *C.cliproxy_host_api, plugin *C.cliproxy_plugin_api) (rc C.int) {
	defer func() {
		if recover() != nil {
			logError("initialization panic recovered")
			rc = 1
		}
	}()
	if host == nil || plugin == nil || host.call == nil || host.free_buffer == nil || uint32(host.abi_version) != pluginabi.ABIVersion {
		return 1
	}
	hostAPI = host
	plugin.abi_version = C.uint32_t(pluginabi.ABIVersion)
	plugin.call = C.cliproxy_plugin_call_fn(C.cliproxyPluginCall)
	plugin.free_buffer = C.cliproxy_plugin_free_fn(C.cliproxyPluginFree)
	plugin.shutdown = C.cliproxy_plugin_shutdown_fn(C.cliproxyPluginShutdown)
	return 0
}

//export cliproxyPluginCall
func cliproxyPluginCall(method *C.char, request *C.uint8_t, requestLen C.size_t, response *C.cliproxy_buffer) (rc C.int) {
	if response == nil {
		return 1
	}
	response.ptr = nil
	response.len = 0
	defer func() {
		if recover() != nil {
			logError("ABI panic recovered; request unchanged")
			writeResponse(response, envelope{OK: true, Result: pluginapi.RequestInterceptResponse{}})
			rc = 0
		}
	}()
	if method == nil || uint64(requestLen) > 2147483647 || (request == nil && requestLen != 0) {
		logError("invalid ABI request; request unchanged")
		return 1
	}
	var raw []byte
	if request != nil && requestLen > 0 {
		raw = C.GoBytes(unsafe.Pointer(request), C.int(requestLen))
	}
	result, err := dispatch(C.GoString(method), raw)
	if err != nil {
		writeResponse(response, envelope{Error: map[string]string{"code": "plugin_error", "message": "plugin operation failed"}})
		return 1
	}
	writeResponse(response, envelope{OK: true, Result: result})
	return 0
}

//export cliproxyPluginFree
func cliproxyPluginFree(ptr unsafe.Pointer, _ C.size_t) {
	defer func() {
		if recover() != nil {
			logError("free-buffer panic recovered")
		}
	}()
	if ptr != nil {
		C.free(ptr)
	}
}

//export cliproxyPluginShutdown
func cliproxyPluginShutdown() {
	defer func() {
		if recover() != nil {
			logError("shutdown panic recovered")
		}
	}()
	state.Lock()
	defer state.Unlock()
	if err := removeOwnStatus(state.cfg.StatusPath, state.loadedAt); err != nil {
		logError("cannot remove shutdown status")
	}
	state.info = nil
	state.policy = policyFile{}
}

// An empty interception result preserves the original body in the host. Never
// return a partially rewritten body after an internal error or recovered panic.
func dispatch(method string, raw []byte) (result any, err error) {
	interceptor := method == pluginabi.MethodRequestInterceptAfter || method == pluginabi.MethodRequestInterceptBefore
	defer func() {
		if recover() != nil {
			logError("dispatch panic recovered; request unchanged")
			if method == pluginabi.MethodPluginRegister || method == pluginabi.MethodPluginReconfigure {
				clearStatus()
			}
			if interceptor {
				result, err = pluginapi.RequestInterceptResponse{}, nil
			} else {
				result, err = nil, fmt.Errorf("plugin panic recovered")
			}
		}
	}()
	result, err = handleMethod(method, raw)
	if err != nil {
		logError("plugin operation failed; request unchanged")
		if method == pluginabi.MethodPluginRegister || method == pluginabi.MethodPluginReconfigure {
			clearStatus()
		}
		if interceptor {
			return pluginapi.RequestInterceptResponse{}, nil
		}
	}
	return
}

func writeResponse(out *C.cliproxy_buffer, v any) {
	raw, err := json.Marshal(v)
	if err != nil {
		return
	}
	out.ptr = C.CBytes(raw)
	out.len = C.size_t(len(raw))
}
func decodeConfig(raw []byte) (pluginConfig, error) {
	cfg := pluginConfig{PolicyPath: "/var/lib/personal-codex-router/fast-mode-policy.json", StatusPath: "/var/lib/cliproxyapi/fast-mode-plugin-status.json", MapUnsupportedUltrafast: true}
	if err := yaml.Unmarshal(raw, &cfg); err != nil {
		return cfg, err
	}
	if !filepath.IsAbs(cfg.PolicyPath) || !filepath.IsAbs(cfg.StatusPath) || filepath.Clean(cfg.PolicyPath) == filepath.Clean(cfg.StatusPath) {
		return cfg, fmt.Errorf("distinct absolute policy_path and status_path required")
	}
	return cfg, nil
}

func handleMethod(method string, raw []byte) (any, error) {
	switch method {
	case pluginabi.MethodPluginRegister, pluginabi.MethodPluginReconfigure:
		var req struct {
			ConfigYAML []byte `json:"config_yaml"`
		}
		if err := json.Unmarshal(raw, &req); err != nil {
			return nil, err
		}
		cfg, err := decodeConfig(req.ConfigYAML)
		if err != nil {
			return nil, err
		}
		if err := configure(cfg); err != nil {
			return nil, err
		}

		return map[string]any{"schema_version": pluginabi.SchemaVersion, "metadata": pluginapi.Metadata{Name: "fast-mode-policy", Version: version, Author: "personal-codex-router", GitHubRepository: "https://github.com/router-for-me/CLIProxyAPI"}, "capabilities": map[string]bool{"request_interceptor": true}}, nil
	case pluginabi.MethodRequestInterceptBefore:
		return pluginapi.RequestInterceptResponse{}, nil
	case pluginabi.MethodRequestInterceptAfter:
		var req pluginapi.RequestInterceptRequest
		if err := json.Unmarshal(raw, &req); err != nil {
			return nil, err
		}
		if !strings.EqualFold(req.ToFormat, "codex") {
			return pluginapi.RequestInterceptResponse{}, nil
		}
		warnWebsocket(req.Metadata, strings.EqualFold(req.Headers.Get("Upgrade"), "websocket"), req.RequestID)
		policy, cfg := lookupPolicy(req.Metadata)
		model := requestModel(req.Model, req.Body)
		body, err := rewrite(req.Body, policy, cfg.MapUnsupportedUltrafast, model, req.SourceFormat)
		if err != nil {
			return nil, err
		}
		if cfg.Audit {
			level := policy.level()
			if level == "" {
				level = "v1"
			}
			hostLog("info", "fast-mode-policy decision", map[string]any{"auth_index": auditAuthIndex(req.Metadata["selected_auth_index"]), "level": level, "ultrafast_model": policy.allowsUltrafast(model), "input_tier": tier(req.Body), "output_tier": tier(body)})
		}
		if bytes.Equal(body, req.Body) {
			return pluginapi.RequestInterceptResponse{}, nil
		}
		return pluginapi.RequestInterceptResponse{Body: body}, nil
	case pluginabi.MethodPluginQuiesce:
		return struct{}{}, nil
	default:
		return nil, fmt.Errorf("unsupported method %s", method)
	}
}
func lookupPolicy(meta map[string]any) (accountPolicy, pluginConfig) {
	state.Lock()
	defer state.Unlock()
	info, err := os.Stat(state.cfg.PolicyPath)
	if err != nil {
		state.info = nil
		state.policy = policyFile{}
		logError("policy missing or unreadable; using default account policy")
	} else if state.info == nil || !info.ModTime().Equal(state.info.ModTime()) || info.Size() != state.info.Size() || info.Mode() != state.info.Mode() || !os.SameFile(info, state.info) {
		state.info = info
		state.policy = policyFile{}
		// Read a bounded regular file; refuse special files that could block the host.
		if !info.Mode().IsRegular() || info.Size() > 4<<20 {
			logError("policy is not a bounded regular file; using default account policy")
		} else {
			raw, err := os.ReadFile(state.cfg.PolicyPath)
			var next policyFile
			if err != nil || json.Unmarshal(raw, &next) != nil || bytes.Equal(bytes.TrimSpace(raw), []byte("null")) {
				logError("policy unreadable or malformed; using default account policy")
			} else {
				state.policy = next
			}
		}
	}
	if idx, ok := meta["selected_auth_index"].(string); ok {
		if p, found := state.policy.Accounts[idx]; found {
			return p, state.cfg
		}
	}
	// Native file-backed Codex auth.ID is the path relative to auth-dir. No auth metadata
	// or FileName is exposed to this hook; only use this fallback for JSON file IDs.
	if id, ok := meta["selected_auth_id"].(string); ok && strings.HasSuffix(strings.ToLower(id), ".json") {
		if p, found := state.policy.Files[filepath.Base(id)]; found {
			return p, state.cfg
		}
	}
	return accountPolicy{}, state.cfg
}

// rewrite edits only fast, priority and ultrafast tiers. It never adds a tier:
// an absent tier stays absent. model must already be normalized.
func rewrite(body []byte, p accountPolicy, mapUltra bool, model, sourceFormat string) ([]byte, error) {
	var obj map[string]json.RawMessage
	if err := json.Unmarshal(body, &obj); err != nil {
		return nil, err
	}
	if obj == nil {
		return nil, fmt.Errorf("request must be a JSON object")
	}
	var value string
	if json.Unmarshal(obj["service_tier"], &value) != nil {
		return body, nil
	}
	requested := strings.ToLower(strings.TrimSpace(value))
	if requested != "fast" && requested != "priority" && requested != "ultrafast" {
		return body, nil
	}
	switch p.level() {
	case "normal":
		return sjson.DeleteBytes(body, "service_tier")
	case "fast":
		if requested == "ultrafast" {
			return sjson.SetBytes(body, "service_tier", "priority")
		}
		return body, nil
	case "ultrafast":
		// Upgrades are allowlist-only and fail closed: an upstream plan or model
		// rejection would cool the model down for 12 hours.
		eligible := p.allowsUltrafast(model) && passesUltrafast(sourceFormat)
		if requested == "ultrafast" {
			if eligible {
				return body, nil
			}
			return sjson.SetBytes(body, "service_tier", "priority")
		}
		if eligible {
			return sjson.SetBytes(body, "service_tier", "ultrafast")
		}
		return body, nil
	}
	// Schema 1 entries, unknown accounts and a missing or malformed policy.
	switch requested {
	case "fast", "priority", "ultrafast":
		if p.AllowFast != nil && !*p.AllowFast {
			return sjson.DeleteBytes(body, "service_tier")
		}
		if strings.EqualFold(strings.TrimSpace(value), "ultrafast") && mapUltra && !p.SupportsUltrafast {
			return sjson.SetBytes(body, "service_tier", "priority")
		}
	}
	return body, nil
}

// Audit values must come from a closed vocabulary, never from request JSON.
func tier(body []byte) string {
	var obj map[string]json.RawMessage
	if json.Unmarshal(body, &obj) != nil || obj == nil {
		return "invalid"
	}
	raw, ok := obj["service_tier"]
	if !ok {
		return "absent"
	}
	var value any
	if json.Unmarshal(raw, &value) != nil {
		return "invalid"
	}
	label, ok := value.(string)
	if !ok {
		return "non-string"
	}
	switch label = strings.ToLower(strings.TrimSpace(label)); label {
	case "normal", "priority", "fast", "ultrafast", "default", "auto", "absent":
		return label
	default:
		return "invalid"
	}
}

// Native auth indexes are 16 hex digits. Never log arbitrary auth IDs or paths.
func auditAuthIndex(value any) string {
	if value == nil {
		return "absent"
	}
	index, ok := value.(string)
	if !ok || len(index) != 16 {
		return "invalid"
	}
	for _, char := range index {
		if !(char >= '0' && char <= '9' || char >= 'a' && char <= 'f' || char >= 'A' && char <= 'F') {
			return "invalid"
		}
	}
	return strings.ToLower(index)
}
func warnWebsocket(meta map[string]any, marker bool, requestID string) {
	session, _ := meta["execution_session_id"].(string)
	if !marker && session == "" {
		return
	}
	if session == "" {
		session, _ = meta["canonical_session_id"].(string)
	}
	if session == "" {
		session = requestID
	}
	if session == "" {
		session = "unknown-websocket"
	}
	// Bound each deduplication key without retaining request-derived session text.
	session = fmt.Sprintf("%x", sha256.Sum256([]byte(session)))
	shouldWarn := func() bool {
		state.Lock()
		defer state.Unlock()
		if state.warned[session] {
			return false
		}
		// Bound memory and globally limit warnings to one per second.
		if len(state.warned) >= 65536 || time.Since(state.lastWarning) < time.Second {
			return false
		}
		state.warned[session] = true
		state.lastWarning = time.Now()
		return true
	}()
	if !shouldWarn {
		return
	}

	hostLog("warning", "fast-mode-policy: follow-up websocket turns bypass the fast-mode policy", nil)
}

var hostLog = func(level, message string, fields map[string]any) {
	defer func() { _ = recover() }()
	if hostAPI == nil {
		fmt.Fprintln(os.Stderr, message)
		return
	}
	raw, _ := json.Marshal(map[string]any{"level": level, "message": message, "fields": fields})
	method := C.CString(pluginabi.MethodHostLog)
	defer C.free(unsafe.Pointer(method))
	data := C.CBytes(raw)
	defer C.free(data)
	var response C.cliproxy_buffer
	C.host_call(hostAPI, method, data, C.size_t(len(raw)), &response)
	C.host_free(hostAPI, &response)
}

var errorMu sync.Mutex
var lastError time.Time

func logError(message string) {
	errorMu.Lock()
	if time.Since(lastError) < time.Second {
		errorMu.Unlock()
		return
	}
	lastError = time.Now()
	errorMu.Unlock()
	hostLog("error", "fast-mode-policy: "+message, nil)
}

type pluginStatus struct {
	Plugin                  string `json:"plugin"`
	Version                 string `json:"version"`
	PID                     int    `json:"pid"`
	LoadedAt                string `json:"loaded_at"`
	PolicyPath              string `json:"policy_path"`
	MapUnsupportedUltrafast bool   `json:"map_unsupported_ultrafast"`
	Audit                   bool   `json:"audit"`
	PolicySchema            int    `json:"policy_schema"`
}

func writeStatus(cfg pluginConfig, loadedAt string) error {
	raw, err := json.Marshal(pluginStatus{"fast-mode-policy", version, os.Getpid(), loadedAt, cfg.PolicyPath, cfg.MapUnsupportedUltrafast, cfg.Audit, policySchema})
	if err != nil {
		return err
	}
	f, err := os.CreateTemp(filepath.Dir(cfg.StatusPath), ".fast-mode-status-*")
	if err != nil {
		return err
	}
	name := f.Name()
	defer os.Remove(name)
	defer f.Close()
	if err = f.Chmod(0640); err != nil {
		return err
	}
	if _, err = f.Write(append(raw, '\n')); err != nil {
		return err
	}
	if err = f.Sync(); err != nil {
		return err
	}
	if err = f.Close(); err != nil {
		return err
	}
	if err = os.Rename(name, cfg.StatusPath); err != nil {
		return err
	}
	dir, err := os.Open(filepath.Dir(cfg.StatusPath))
	if err != nil {
		return err
	}
	defer dir.Close()
	return dir.Sync()
}

// An old library's shutdown must not remove a replacement library's status.
func removeOwnStatus(path, loadedAt string) error {
	if path == "" || loadedAt == "" {
		return nil
	}
	f, err := os.Open(path)
	if os.IsNotExist(err) {
		return nil
	}
	if err != nil {
		return err
	}
	defer f.Close()
	var status pluginStatus
	if err = json.NewDecoder(io.LimitReader(f, 8192)).Decode(&status); err != nil {
		return err
	}
	if status.Plugin == "fast-mode-policy" && status.PID == os.Getpid() && status.LoadedAt == loadedAt {
		return os.Remove(path)
	}
	return nil
}

func configure(cfg pluginConfig) error {
	state.Lock()
	defer state.Unlock()
	loadedAt := time.Now().UTC().Format(time.RFC3339Nano)
	if err := writeStatus(cfg, loadedAt); err != nil {
		// Publication may have reached rename before directory fsync failed.
		_ = removeOwnStatus(cfg.StatusPath, loadedAt)
		return fmt.Errorf("cannot publish plugin status")
	}
	if state.cfg.StatusPath != cfg.StatusPath {
		if err := removeOwnStatus(state.cfg.StatusPath, state.loadedAt); err != nil {
			logError("cannot remove previous status")
		}
	}
	state.cfg = cfg
	state.loadedAt = loadedAt
	state.info = nil
	state.policy = policyFile{}
	return nil
}

func clearStatus() {
	defer func() {
		if recover() != nil {
			logError("status cleanup panic recovered")
		}
	}()
	state.Lock()
	defer state.Unlock()
	if err := removeOwnStatus(state.cfg.StatusPath, state.loadedAt); err != nil {
		logError("cannot clear failed registration status")
	}
}

import { spawn } from "node:child_process"
import { fileURLToPath } from "node:url"
import path from "node:path"
import { existsSync, readdirSync } from "node:fs"
import { type Plugin, tool } from "@opencode-ai/plugin"

type BridgeResult = {
  exitCode: number
  stdout: string
  stderr: string
  json: unknown
}

type IdeaCliResult = BridgeResult & {
  argv: string[]
}

const pluginFile = fileURLToPath(import.meta.url)
const pluginRoot = path.resolve(path.dirname(pluginFile), "..", "..")
const bridgeFile = path.join(pluginRoot, "runtime", "python", "bridge", "smell_bridge.py")
const bundledIdeaRefactorCli = path.resolve(pluginRoot, "bin", "idea-refactor")

function addOptional(args: string[], flag: string, value?: string) {
  if (value && value.trim()) {
    args.push(flag, value)
  }
}

function envDefault(name: string): string | undefined {
  const value = process.env[name]
  return value && value.trim() ? value : undefined
}

function jsonObjectShape(description: string) {
  return tool.schema.record(tool.schema.string(), tool.schema.unknown()).optional().describe(description)
}

function withBatchDefaults(input: {
  projectRoot?: string
  language?: string
  smell?: string
  location?: string
  smellEvidence?: string
  verificationMode?: string
  [key: string]: unknown
}) {
  const envProjectRoot = envDefault("SMELL_PROJECT_ROOT")
  const envCanonicalProjectRoot = envDefault("SMELL_CANONICAL_PROJECT_ROOT")
  const envLanguage = envDefault("SMELL_LANGUAGE")
  const envSmell = envDefault("SMELL_SMELL")
  const envLocation = envDefault("SMELL_LOCATION")
  const envEvidence = envDefault("SMELL_EVIDENCE")
  const envVerificationMode = envDefault("SMELL_VERIFICATION_MODE")
  const envSampleTestLocation = envDefault("SMELL_SAMPLE_TEST_LOCATION")
  const envSampleTestCommand = envDefault("SMELL_SAMPLE_TEST_COMMAND")
  const hasBatchIdentity = Boolean(envProjectRoot && envSmell && envLocation)
  return {
    ...input,
    projectRoot: hasBatchIdentity ? envProjectRoot! : input.projectRoot,
    projectOverrideRoot: envCanonicalProjectRoot,
    language: input.language || envLanguage,
    smell: hasBatchIdentity ? envSmell! : input.smell,
    location: hasBatchIdentity ? envLocation! : input.location,
    smellEvidence: input.smellEvidence || envEvidence,
    verificationMode: input.verificationMode || envVerificationMode,
    sampleTestLocation: envSampleTestLocation,
    sampleTestCommand: envSampleTestCommand,
  }
}

function commonArgs(input: {
  projectRoot: string
  projectOverrideRoot?: string
  language?: string
  smell: string
  location: string
  config?: string
  projects?: string
  smellEvidence?: string
  guardContextJson?: string
  verificationMode?: string
  sampleTestLocation?: string
  sampleTestCommand?: string
}): string[] {
  const args = [
    "--project-root",
    input.projectRoot,
    "--smell",
    input.smell,
    "--location",
    input.location,
  ]
  addOptional(args, "--language", input.language)
  addOptional(args, "--project-override-root", input.projectOverrideRoot)
  addOptional(args, "--config", input.config)
  addOptional(args, "--projects", input.projects)
  addOptional(args, "--smell-evidence", input.smellEvidence)
  addOptional(args, "--guard-context-json", input.guardContextJson)
  addOptional(args, "--verification-mode", input.verificationMode)
  addOptional(args, "--sample-test-location", input.sampleTestLocation)
  addOptional(args, "--sample-test-command", input.sampleTestCommand)
  return args
}

const MAX_STDOUT_STDERR_LEN = 4000

// --- session.idle limited auto-continuation (off by default) ---------------
//
// This is an interactive-mode fallback. It never activates in `opencode run`
// single-shot mode or in batch runner environments. smell_verify.execute only
// updates in-process state and metadata; the actual client.session.promptAsync
// call happens in the plugin `event` hook after a `session.idle` for the same
// session. The whole mechanism defaults to "off" and requires both
// SMELL_IDLE_CONTINUE_MODE=interactive and smell_verify({ autoContinue: true }).

const SMELL_IDLE_CONTINUE_PREFIX = "[smell-auto-continue"
const MAX_IDLE_CONTINUE_ATTEMPTS = 2 // hard limit, cannot be raised by model args
const IDLE_CONTINUE_STATE_TTL_MS = 30 * 60 * 1000
const ALLOWED_AGENTS = new Set(["java-refactor-agent", "java-refactor-agent-idea"])
// Conservative allowlist of repairable failure categories. These are the
// category strings the Python bridge actually emits from
// `_classify_failure_pack`. BUILD_FAILED is intentionally NOT listed: only an
// explicit BUILD_COMPILE_ERROR classification from failure_pack is treated as
// repairable compile trouble. Test regressions come back as
// TEST_BEHAVIOR_REGRESSION or TEST_REFLECTION_ENTRY_STALE, so those (not the
// never-emitted literal "TEST_FAILED") are what we allow.
// Dependency / offline / auth / provider / config / tool / infrastructure /
// timeout / unknown failures are never continued.
const REPAIRABLE_CATEGORIES = new Set([
  "SMELL_GUARD_FAILED",
  "BUILD_COMPILE_ERROR",
  "TEST_BEHAVIOR_REGRESSION",
  "TEST_REFLECTION_ENTRY_STALE",
  "SAMPLE_TEST_FAILED",
])

// OpenCode subcommands that run non-interactively or headlessly and therefore
// must never receive an auto-continuation. The bare `opencode` invocation
// (TUI, no subcommand) is interactive and is NOT in this set.
const NONINTERACTIVE_SUBCOMMANDS = new Set(["run", "serve", "web", "attach"])

type IdleContinueMode = "off" | "shadow" | "interactive"

type FailureClassification = {
  ok: boolean
  category: string
  verifyStatus: string
  highlights: string[]
  artifactPaths: string[]
}

type ContinuationState = {
  taskKey: string
  generation: number
  dispatchedGeneration: number
  attempt: number
  pending: boolean
  dispatching: boolean
  agent: string
  directory: string
  failureCategory: string
  verifyStatus: string
  failureHighlights: string[]
  artifactPaths: string[]
  updatedAt: number
}

function idleContinueMode(env: NodeJS.ProcessEnv = process.env): IdleContinueMode {
  const raw = typeof env?.SMELL_IDLE_CONTINUE_MODE === "string" ? env.SMELL_IDLE_CONTINUE_MODE.trim() : ""
  if (raw === "shadow") return "shadow"
  if (raw === "interactive") return "interactive"
  return "off" // default; unrecognized values are treated as off
}

function isBatchEnvironment(env: NodeJS.ProcessEnv = process.env): boolean {
  if (!env) return false
  if (env.SMELL_BATCH_RUN === "1") return true
  const projectRoot = typeof env.SMELL_PROJECT_ROOT === "string" ? env.SMELL_PROJECT_ROOT.trim() : ""
  return Boolean(projectRoot)
}

// Identify non-interactive OpenCode invocations (`opencode run`, `serve`,
// `web`, `attach`) from argv. The bare `opencode` command (TUI, no
// subcommand) is interactive and must return false so the README's
// `SMELL_IDLE_CONTINUE_MODE=interactive opencode` actually enables continuation.
// Conservative: only when we can positively identify the opencode executable do
// we trust the subcommand; otherwise (unrecognizable argv) we treat it as run.
function isOpendcodeRunMode(argv: readonly string[] = process.argv): boolean {
  if (!Array.isArray(argv) || argv.length === 0) return true
  let opencodeIndex = -1
  for (let i = 0; i < argv.length; i += 1) {
    const token = String(argv[i] || "")
    if (!token) continue
    const base = path.basename(token)
    if (base === "opencode" || base === "opencode.exe") {
      opencodeIndex = i
      break
    }
  }
  if (opencodeIndex < 0) {
    // No recognizable opencode executable in argv. Conservative: treat as run.
    return true
  }
  const next = argv[opencodeIndex + 1]
  if (typeof next !== "string") {
    // Bare `opencode` with no subcommand -> interactive TUI. Do NOT treat as run.
    return false
  }
  return NONINTERACTIVE_SUBCOMMANDS.has(next)
}

// The bridge emits failure_pack.artifact_paths as an object (name -> path), not
// a string array. Accept both shapes defensively.
function artifactPathsFrom(value: unknown): string[] {
  if (Array.isArray(value)) {
    return value.map((item) => (typeof item === "string" ? item : "")).filter((item) => item.length > 0)
  }
  if (value && typeof value === "object") {
    const entries = Object.values(value as Record<string, unknown>)
    return entries
      .map((item) => (typeof item === "string" ? item : ""))
      .filter((item) => item.length > 0)
  }
  return []
}

function asStringArray(value: unknown): string[] {
  if (!Array.isArray(value)) return []
  return value
    .map((item) => (typeof item === "string" ? item : ""))
    .filter((item) => item.length > 0)
}

// Redact secrets and credential-like values from text before it can appear in a
// visible synthetic message. Match by structure (key/header scheme + value),
// not by guessing credential length, so short or quoted values are covered too.
//
// Covered forms:
//   api_key="value"   api_key='value'   api_key=value   api-key: value
//   TOKEN="value"     ACCESS_TOKEN='value'   secret=value
//   Authorization: Basic <anything-to-end-of-line>
//   Authorization: Bearer <anything-to-end-of-line>
//   bearer <token>   token <token>   {env:NAME} stays, but NAME=value leaks
//
// Conservative: when a key/header is recognized, the WHOLE value is redacted,
// including quoted values and scheme-prefixed credentials.

const REDACT_VALUE_CHARS = String.raw`[^'"\\]`

// KEY = value, KEY: value, KEY="value", KEY='value' for known credential key
// names. Captures the key prefix up to and including the separator so we can
// keep the key name and redact the value only.
const REDACT_KEY_RE = new RegExp(
  String.raw`\b(authorization|api[_-]?key|apikey|secret|access[_-]?token|refresh[_-]?token|auth[_-]?token|password|passwd|passwd64|private[_-]?key|client[_-]?secret)` +
    String.raw`\b(\s*[:=]\s*)("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|` +
    REDACT_VALUE_CHARS +
    String.raw`+)`,
  "gi",
)

// Authorization: <scheme> <credentials>  — redact the credentials after the scheme.
// Handles "Authorization: Basic abc...", "Authorization: Bearer xyz ...".
const REDACT_AUTH_SCHEME_RE = new RegExp(
  String.raw`\b(authorization)\b(\s*:\s*)([A-Za-z][A-Za-z0-9._-]*)(\s+)("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|\S+)`,
  "gi",
)

// Standalone scheme tokens that carry a trailing credential: "Bearer xxx".
const REDACT_SCHEME_TOKEN_RE = new RegExp(
  String.raw`\b(bearer|token|basic)\b(\s+)("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|\S+)`,
  "gi",
)

// UPPERCASE_ENV_NAME=<value> assignments leaked into logs. Covers quoted and
// unquoted values; "NAME" is preserved, the value is redacted.
const REDACT_ENV_ASSIGN_RE = new RegExp(
  String.raw`\b([A-Z][A-Z0-9_]*)(\s*=\s*)("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|\S+)`,
  "g",
)

function redactValue(value: string): string {
  // For quoted values, redact the inner content but keep quote boundaries so
  // log formatting stays readable.
  if (value.length >= 2 && (value[0] === '"' || value[0] === "'")) {
    return `${value[0]}[REDACTED]${value[0]}`
  }
  return "[REDACTED]"
}

function redactSecrets(input: string): string {
  if (typeof input !== "string" || input.length === 0) return ""
  let out = input
  out = out.replace(REDACT_KEY_RE, (_m, key: string, sep: string, value: string) => `${key}${sep}${redactValue(value)}`)
  out = out.replace(REDACT_AUTH_SCHEME_RE, (_m, key: string, sep: string, scheme: string, _ws: string, _cred: string) => `${key}${sep}${scheme} [REDACTED]`)
  out = out.replace(REDACT_SCHEME_TOKEN_RE, (_m, scheme: string, ws: string, _value: string) => `${scheme}${ws}[REDACTED]`)
  out = out.replace(REDACT_ENV_ASSIGN_RE, (_m, name: string, sep: string, _value: string) => `${name}${sep}[REDACTED]`)
  return out
}

function classifyFailureForContinue(failurePack: unknown): FailureClassification {
  const empty: FailureClassification = {
    ok: false,
    category: "",
    verifyStatus: "",
    highlights: [],
    artifactPaths: [],
  }
  if (!failurePack || typeof failurePack !== "object" || Array.isArray(failurePack)) {
    return empty
  }
  const pack = failurePack as Record<string, unknown>
  const category = typeof pack.failure_category === "string" ? pack.failure_category.trim() : ""
  const verifyStatus = typeof pack.verify_status === "string" ? pack.verify_status.trim() : ""
  const highlights = asStringArray(pack.highlights)
  const artifactPaths = artifactPathsFrom(pack.artifact_paths)
  return {
    ok: REPAIRABLE_CATEGORIES.has(category),
    category,
    verifyStatus,
    highlights,
    artifactPaths,
  }
}

function makeTaskKey(projectRoot: string, smell: string, location: string): string {
  return [String(projectRoot || ""), String(smell || ""), String(location || "")].join("|")
}

function buildContinuationMessage(state: ContinuationState): string {
  const attempt = Math.max(1, Math.min(state.attempt + 1, MAX_IDLE_CONTINUE_ATTEMPTS))
  const lines: string[] = []
  lines.push(`${SMELL_IDLE_CONTINUE_PREFIX} ${attempt}/${MAX_IDLE_CONTINUE_ATTEMPTS}]`)
  lines.push("")
  lines.push("The previous smell_verify result was not accepted.")
  lines.push(`Status: ${state.verifyStatus || "FAILED"}.`)
  lines.push(`Failure category: ${state.failureCategory || "UNKNOWN"}.`)
  lines.push("")
  const highlights = state.failureHighlights.slice(0, 3)
  if (highlights.length) {
    lines.push("Failure highlights:")
    for (const h of highlights) {
      const redacted = redactSecrets(h)
      const trimmed = redacted.length > 200 ? `${redacted.slice(0, 200)}...` : redacted
      lines.push(`- ${trimmed}`)
    }
    lines.push("")
  }
  const paths = state.artifactPaths.slice(0, 3)
  if (paths.length) {
    lines.push("Artifact paths:")
    for (const p of paths) lines.push(`- ${redactSecrets(p)}`)
    lines.push("")
  }
  lines.push("Read the latest failure_pack and make one narrow corrective edit.")
  lines.push("Then call smell_verify again. Do not repeat the previous edit without")
  lines.push("new evidence. Do not modify or weaken tests.")
  let message = lines.join("\n")
  // Hard cap near 2 KB to keep the synthetic message small.
  const MAX_MSG = 2048
  if (message.length > MAX_MSG) {
    message = `${message.slice(0, MAX_MSG - 32)}\n...[truncated]`
  }
  return message
}

type StdioCarrier = {
  exitCode?: unknown
  stdout?: unknown
  stderr?: unknown
}

function safeStringOutput(value: unknown): string {
  if (typeof value === "string") return value
  if (value === null || value === undefined) return ""
  if (typeof value === "number" || typeof value === "boolean" || typeof value === "bigint") {
    return String(value)
  }
  try {
    return JSON.stringify(value, null, 2)
  } catch {
    try {
      return String(value)
    } catch {
      return ""
    }
  }
}

function truncateText(value: unknown, limit: number = MAX_STDOUT_STDERR_LEN): string {
  const text = typeof value === "string" ? value : safeStringOutput(value)
  if (text.length <= limit) return text
  return `${text.slice(0, limit)}\n...[truncated ${text.length - limit} chars]`
}

function safeJsonStringify(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2)
  } catch {
    try {
      return JSON.stringify(
        {
          success: false,
          status: "OUTPUT_SERIALIZE_FAILED",
          error: "Tool output could not be serialized.",
        },
        null,
        2,
      )
    } catch {
      return '{"success":false,"status":"OUTPUT_SERIALIZE_FAILED"}'
    }
  }
}

function toJsonSafe(value: unknown): unknown {
  if (value === null || value === undefined) return null
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") return value
  if (typeof value === "bigint") return String(value)
  if (Array.isArray(value)) return value.map(toJsonSafe)
  if (typeof value === "object") {
    try {
      JSON.stringify(value)
      return value
    } catch {
      return safeStringOutput(value)
    }
  }
  return safeStringOutput(value)
}

function normalizeStdioFields(result: StdioCarrier): {
  exitCode: number
  stdout: string
  stderr: string
} {
  const rawExit = result.exitCode
  let exitCode = 1
  if (typeof rawExit === "number" && Number.isFinite(rawExit)) {
    exitCode = Math.trunc(rawExit)
  } else if (typeof rawExit === "string" && rawExit.trim() !== "" && Number.isFinite(Number(rawExit))) {
    exitCode = Math.trunc(Number(rawExit))
  }
  return {
    exitCode,
    stdout: typeof result.stdout === "string" ? result.stdout : safeStringOutput(result.stdout),
    stderr: typeof result.stderr === "string" ? result.stderr : safeStringOutput(result.stderr),
  }
}

function normalizeMetadata(
  result: StdioCarrier,
  extra: Record<string, unknown> = {},
): Record<string, unknown> {
  const fields = normalizeStdioFields(result)
  const metadata: Record<string, unknown> = {
    exitCode: fields.exitCode,
    stderr: truncateText(fields.stderr),
    stdout_truncated: fields.stdout.length > MAX_STDOUT_STDERR_LEN,
    ...extra,
  }
  for (const key of Object.keys(metadata)) {
    metadata[key] = toJsonSafe(metadata[key])
  }
  return metadata
}

function buildBridgeOutputPayload(result: BridgeResult): string {
  const fields = normalizeStdioFields(result)
  const stderrSummary = truncateText(fields.stderr)
  const stdoutSummary = truncateText(fields.stdout)
  if (result.json === null || result.json === undefined) {
    return safeJsonStringify({
      success: false,
      status: fields.exitCode === 0 ? "BRIDGE_OUTPUT_NOT_JSON" : "BRIDGE_FAILED",
      error: stderrSummary || "Python bridge did not return a JSON payload.",
      bridge: {
        exitCode: fields.exitCode,
        stderr: stderrSummary,
        stdout_summary: stdoutSummary,
      },
    })
  }
  const jsonPayload =
    result.json && typeof result.json === "object"
      ? (result.json as Record<string, unknown>)
      : { value: result.json }
  const hasStatus = typeof jsonPayload.status === "string" && jsonPayload.status.trim() !== ""
  const hasSuccess = typeof jsonPayload.success === "boolean"
  return safeJsonStringify({
    ...jsonPayload,
    success: hasSuccess ? (jsonPayload.success as boolean) : fields.exitCode === 0,
    status: hasStatus
      ? (jsonPayload.status as string)
      : fields.exitCode === 0
        ? "BRIDGE_OK_NO_STATUS"
        : "BRIDGE_FAILED",
    bridge: {
      exitCode: fields.exitCode,
      stderr: stderrSummary,
    },
  })
}

function normalizeToolResult(
  title: string,
  result: BridgeResult,
  extraMetadata: Record<string, unknown> = {},
): { title: string; output: string; metadata: Record<string, unknown> } {
  return {
    title: typeof title === "string" && title ? title : "Smell tool result",
    output: buildBridgeOutputPayload(result),
    metadata: normalizeMetadata(result, extraMetadata),
  }
}

async function runBridge(worktree: string, args: string[]): Promise<BridgeResult> {
  return await new Promise((resolve) => {
    let stdout = ""
    let stderr = ""
    let settled = false
    const finalize = (exitCode: number) => {
      if (settled) return
      settled = true
      let json: unknown = null
      try {
        json = JSON.parse(stdout)
      } catch {
        json = null
      }
      resolve({ exitCode, stdout, stderr, json })
    }
    const child = spawn("python3", [bridgeFile, ...args], {
      cwd: worktree,
      env: process.env,
      stdio: ["ignore", "pipe", "pipe"],
    })
    child.stdout.on("data", (chunk) => {
      stdout += String(chunk)
    })
    child.stderr.on("data", (chunk) => {
      stderr += String(chunk)
    })
    child.on("error", (error) => {
      if (!stderr) {
        stderr = error instanceof Error ? error.message : String(error)
      }
      finalize(1)
    })
    child.on("close", (code) => {
      finalize(code ?? 1)
    })
  })
}

async function runIdeaCli(worktree: string, cli: string, args: string[]): Promise<IdeaCliResult> {
  return await new Promise((resolve) => {
    const child = spawn(cli, args, {
      cwd: worktree,
      env: process.env,
      stdio: ["ignore", "pipe", "pipe"],
    })
    let stdout = ""
    let stderr = ""
    child.stdout.on("data", (chunk) => {
      stdout += String(chunk)
    })
    child.stderr.on("data", (chunk) => {
      stderr += String(chunk)
    })
    child.on("error", (error) => {
      resolve({
        exitCode: 1,
        stdout,
        stderr: stderr || error.message,
        json: {
          status: "failed",
          diagnostics: [{ code: "IDEA_CLI_SPAWN_FAILED", summary: error.message }],
        },
        argv: args,
      })
    })
    child.on("close", (code) => {
      let json: unknown = null
      try {
        json = JSON.parse(stdout)
      } catch {
        json = {
          status: "failed",
          diagnostics: [{ code: "IDEA_CLI_OUTPUT_PARSE_FAILED", summary: "IDEA CLI output was not valid JSON." }],
          stdout,
        }
      }
      resolve({ exitCode: code ?? 1, stdout, stderr, json, argv: args })
    })
  })
}

function resolveIdeaInput(input: { projectRoot?: string; ideaProjectRoot?: string; ideaRefactorCli?: string } = {}) {
  const language = envDefault("SMELL_LANGUAGE")
  const envIdeaProjectRoot = envDefault("SMELL_IDEA_PROJECT_ROOT")
  const envProjectRoot = envDefault("SMELL_PROJECT_ROOT")
  const projectRoot = input.ideaProjectRoot || envIdeaProjectRoot || input.projectRoot || envProjectRoot
  const projectRootSource = input.ideaProjectRoot
    ? "tool.ideaProjectRoot"
    : envIdeaProjectRoot
      ? "env.SMELL_IDEA_PROJECT_ROOT"
      : input.projectRoot
        ? "tool.projectRoot"
        : "env.SMELL_PROJECT_ROOT"
  const ideaRefactorCli =
    input.ideaRefactorCli || envDefault("SMELL_IDEA_REFACTOR_CLI") || envDefault("IDEA_REFACTOR_CLI") || bundledIdeaRefactorCli
  const wrapperMetadata = {
    language: language || "",
    project_root: projectRoot || "",
    project_root_source: projectRootSource,
  }
  if (language && language !== "java") {
    return {
      ok: false as const,
      projectRoot: projectRoot || "",
      ideaRefactorCli,
      wrapperMetadata,
      result: {
        title: "IDEA refactor unsupported language",
        output: JSON.stringify(
          {
            status: "failed",
            diagnostics: [
              {
                code: "UNSUPPORTED_LANGUAGE_FOR_IDEA",
                summary: `IDEA refactor tools are only supported for Java samples; got ${language}.`,
              },
            ],
          },
          null,
          2,
        ),
        metadata: { exitCode: 1, stderr: "", wrapper: wrapperMetadata },
      },
    }
  }
  if (!projectRoot) {
    return {
      ok: false as const,
      projectRoot: "",
      ideaRefactorCli,
      wrapperMetadata,
      result: {
        title: "IDEA refactor input error",
        output: JSON.stringify(
          {
            status: "failed",
            diagnostics: [{ code: "MISSING_PROJECT_ROOT", summary: "projectRoot is required." }],
          },
          null,
          2,
        ),
        metadata: { exitCode: 1, stderr: "", wrapper: wrapperMetadata },
      },
    }
  }
  return { ok: true as const, projectRoot, ideaRefactorCli, wrapperMetadata }
}

function ideaRuntimeMetadata(worktree: string, projectRoot: string) {
  const serverInfoPath = path.join(projectRoot, ".idea-refactoring", "server.json")
  const serverInfoParent = path.dirname(serverInfoPath)
  let serverInfoParentListing: string[] = []
  try {
    serverInfoParentListing = existsSync(serverInfoParent) ? readdirSync(serverInfoParent).sort() : []
  } catch {
    serverInfoParentListing = []
  }
  return {
    worktree,
    cwd: process.cwd(),
    server_info_path: serverInfoPath,
    server_info_exists: existsSync(serverInfoPath),
    server_info_parent_exists: existsSync(serverInfoParent),
    server_info_parent_listing: serverInfoParentListing,
  }
}

function resolveIdeaFile(file: string, resolvedProjectRoot: string) {
  const rawFile = String(file || "").trim()
  if (!rawFile) {
    return {
      ok: false as const,
      file: "",
      result: {
        title: "IDEA refactor locate",
        output: JSON.stringify(
          {
            status: "failed",
            diagnostics: [{ code: "IDEA_FILE_PATH_NOT_RESOLVED", summary: "file is required." }],
          },
          null,
          2,
        ),
        metadata: { exitCode: 1, stderr: "" },
      },
    }
  }
  if (path.isAbsolute(rawFile)) {
    return { ok: true as const, file: rawFile }
  }
  const ideaCandidate = path.resolve(resolvedProjectRoot, rawFile)
  if (existsSync(ideaCandidate)) {
    return { ok: true as const, file: ideaCandidate }
  }
  const datasetRoot = envDefault("SMELL_PROJECT_ROOT")
  if (datasetRoot) {
    const datasetCandidate = path.resolve(datasetRoot, rawFile)
    if (existsSync(datasetCandidate)) {
      return { ok: true as const, file: datasetCandidate }
    }
  }
  return {
    ok: false as const,
    file: rawFile,
    result: {
      title: "IDEA refactor locate",
      output: JSON.stringify(
        {
          status: "failed",
          diagnostics: [
            {
              code: "IDEA_FILE_PATH_NOT_RESOLVED",
              summary: `Unable to resolve '${rawFile}' under IDEA root '${resolvedProjectRoot}' or dataset root '${datasetRoot || ""}'.`,
            },
          ],
          attempted_paths: [ideaCandidate, datasetRoot ? path.resolve(datasetRoot, rawFile) : ""].filter(Boolean),
        },
        null,
        2,
      ),
      metadata: { exitCode: 1, stderr: "" },
    },
  }
}

function addNumber(args: string[], flag: string, value?: number) {
  if (typeof value === "number") {
    args.push(flag, String(value))
  }
}

function addJson(args: string[], flag: string, value?: Record<string, unknown>) {
  if (value) {
    args.push(flag, JSON.stringify(value))
  }
}

function operationsFrom(payload: unknown): string[] {
  if (!payload || typeof payload !== "object") return []
  const available = (payload as { availableOperations?: unknown }).availableOperations
  if (!Array.isArray(available)) return []
  return available
    .map((item) => {
      if (!item || typeof item !== "object") return ""
      const operation = (item as { operation?: unknown }).operation
      return typeof operation === "string" ? operation : ""
    })
    .filter(Boolean)
}

function operationMatches(payload: unknown, expectedOperation?: string): boolean | undefined {
  if (!expectedOperation) return undefined
  if (operationsFrom(payload).includes(expectedOperation)) return true
  if (payload && typeof payload === "object") {
    const operation = (payload as { operation?: unknown }).operation
    if (operation === expectedOperation) return true
  }
  return false
}

function renderIdeaResult(
  title: string,
  result: IdeaCliResult,
  expectedOperation?: string,
  extraWrapper: Record<string, unknown> = {},
): { title: string; output: string; metadata: Record<string, unknown> } {
  const fields = normalizeStdioFields(result)
  const operationAvailable = operationMatches(result.json, expectedOperation)
  const rawPayload =
    result.json && typeof result.json === "object"
      ? (result.json as Record<string, unknown>)
      : result.json === null || result.json === undefined
        ? null
        : { value: result.json }
  const payloadStatus = rawPayload && typeof rawPayload.status === "string" ? rawPayload.status : ""
  const success =
    fields.exitCode === 0 &&
    payloadStatus !== "failed" &&
    payloadStatus !== "error" &&
    Boolean(rawPayload)
  const status =
    payloadStatus ||
    (success ? "IDEA_OK" : fields.exitCode === 0 ? "IDEA_EMPTY_PAYLOAD" : "IDEA_FAILED")
  return {
    title: typeof title === "string" && title ? title : "IDEA refactor result",
    output: safeJsonStringify({
      success,
      status,
      payload: rawPayload,
      wrapper: toJsonSafe({
        exit_code: fields.exitCode,
        stderr: truncateText(fields.stderr),
        argv_preview: Array.isArray(result.argv) ? result.argv.map(String) : [],
        expected_operation: expectedOperation || "",
        operation_available: operationAvailable,
        ...extraWrapper,
      }),
    }),
    metadata: normalizeMetadata(result),
  }
}

// Create an isolated idle-continuation runtime. Pure-function-driven so the
// self-check can inject a fake client/env/argv without touching real state.
// Production code calls this with the real client/process.env/process.argv.
function createIdleContinueRuntime(options: {
  client?: { session: { promptAsync: (opts: unknown) => Promise<unknown> } }
  env?: NodeJS.ProcessEnv
  argv?: readonly string[]
  log?: (msg: string, details?: unknown) => void
}) {
  const env = options.env || process.env
  const argv = options.argv || process.argv
  const log = options.log || (() => {})
  const states = new Map<string, ContinuationState>()
  let disposed = false
  let lastDispatchError = ""

  function cleanupStale(now: number = Date.now()) {
    for (const [id, state] of states) {
      if (now - state.updatedAt > IDLE_CONTINUE_STATE_TTL_MS) states.delete(id)
    }
  }

  function clearSession(sessionID: string) {
    if (sessionID) states.delete(sessionID)
  }

  function clearAll() {
    states.clear()
  }

  function dispose() {
    disposed = true
    clearAll()
  }

  function peek(sessionID: string): ContinuationState | undefined {
    return states.get(sessionID)
  }

  // Called from smell_verify.execute after a bridge result is normalized.
  // Returns the auto_continuation metadata to attach to the tool result.
  function recordFromBridgeOutput(input: {
    sessionID: string
    agent: string
    directory: string
    taskKey: string
    output: string
    autoContinue: boolean
  }): {
    mode: IdleContinueMode
    enabled: boolean
    autoContinue: boolean
    attempt: number
    maxAttempts: number
    generation: number
    status: string
    category: string
    dispatched: boolean
  } {
    cleanupStale()
    const mode = idleContinueMode(env)
    const enabled = mode !== "off"
    let status = ""
    let category = ""
    let passed = false
    let jsonParsed = false
    let failurePack: unknown = null
    try {
      const parsed = JSON.parse(input.output) as Record<string, unknown>
      jsonParsed = true
      status = typeof parsed.status === "string" ? parsed.status : ""
      if (typeof parsed.success === "boolean") passed = parsed.success
      category = typeof parsed.failure_category === "string" ? parsed.failure_category : ""
      failurePack = parsed.failure_pack
    } catch {
      // Non-JSON bridge output: never continue.
    }

    const existing = states.get(input.sessionID)
    // The continuation attempt budget is per-session and is only reset by a
    // genuine new user message (handleChatMessage). It must NOT be reset by a
    // taskKey/location change, otherwise the model can bypass the 2-round cap by
    // shifting the reported location between calls.
    const attempt = existing ? existing.attempt : 0

    const base = {
      mode,
      enabled,
      autoContinue: Boolean(input.autoContinue),
      attempt,
      maxAttempts: MAX_IDLE_CONTINUE_ATTEMPTS,
      generation: existing ? existing.generation : 0,
      status,
      category: category || (existing ? existing.failureCategory : ""),
      dispatched: existing ? existing.dispatchedGeneration === existing.generation : false,
    }

    // PASS: clear state, no continuation.
    if (passed || status === "PASS") {
      states.delete(input.sessionID)
      return { ...base, dispatched: false }
    }

    // Any new verify result that cannot arm continuation must revoke a stale
    // pending from a previous generation, otherwise a later session.idle would
    // resume against an outdated failure_pack. This covers: non-JSON output,
    // autoContinue=false, non-interactive mode, and non-repairable categories.
    const revokePending = () => {
      if (existing && existing.pending) {
        existing.pending = false
        existing.dispatching = false
        existing.updatedAt = Date.now()
      }
    }

    if (!jsonParsed) {
      revokePending()
      return { ...base, dispatched: false }
    }

    if (!enabled || !input.autoContinue) {
      revokePending()
      return base
    }

    const classification = classifyFailureForContinue(failurePack)
    if (!classification.ok) {
      log("smell-idle-continue skip non-repairable", {
        sessionID: input.sessionID,
        category: classification.category,
        status: classification.verifyStatus,
      })
      revokePending()
      return { ...base, category: classification.category || category, dispatched: false }
    }

    // A new repairable failure arms continuation for this generation. taskKey is
    // recorded for diagnostics only; it does NOT reset the attempt budget.
    const nextGeneration = existing ? existing.generation + 1 : 1
    // If an in-flight dispatch exists for the previous generation, preserve the
    // SAME state object reference (mutate in place) so the pending .then()/.catch()
    // callbacks still update the live object rather than an orphan. Otherwise
    // build a fresh object.
    const hasInflightDispatch = Boolean(existing && existing.dispatching)
    const nextState: ContinuationState = existing && hasInflightDispatch
      ? existing!
      : {
          taskKey: input.taskKey,
          generation: nextGeneration,
          dispatchedGeneration: existing ? existing.dispatchedGeneration : -1,
          attempt,
          pending: true,
          dispatching: false,
          agent: input.agent,
          directory: input.directory,
          failureCategory: classification.category,
          verifyStatus: classification.verifyStatus || status,
          failureHighlights: classification.highlights,
          artifactPaths: classification.artifactPaths,
          updatedAt: Date.now(),
        }
    // When mutating in place, update the fields that changed.
    if (hasInflightDispatch) {
      nextState.taskKey = input.taskKey
      nextState.generation = nextGeneration
      nextState.pending = true
      nextState.agent = input.agent
      nextState.directory = input.directory
      nextState.failureCategory = classification.category
      nextState.verifyStatus = classification.verifyStatus || status
      nextState.failureHighlights = classification.highlights
      nextState.artifactPaths = classification.artifactPaths
      nextState.updatedAt = Date.now()
    }
    states.set(input.sessionID, nextState)
    log("smell-idle-continue armed", {
      sessionID: input.sessionID,
      generation: nextState.generation,
      taskKey: nextState.taskKey,
      category: nextState.failureCategory,
      attempt: nextState.attempt,
    })
    return {
      ...base,
      attempt: nextState.attempt,
      generation: nextState.generation,
      category: nextState.failureCategory,
      dispatched: nextState.dispatchedGeneration === nextState.generation,
    }
  }

  // Called from the plugin `event` hook on session.idle. Returns true if a
  // promptAsync dispatch was actually performed.
  function handleIdle(sessionID: string): boolean {
    cleanupStale()
    if (disposed) return false
    const mode = idleContinueMode(env)
    if (mode === "off") return false
    if (isBatchEnvironment(env) || isOpendcodeRunMode(argv)) {
      if (mode === "shadow") {
        log("smell-idle-continue skip batch/run mode (shadow)", { sessionID })
      }
      return false
    }
    const state = states.get(sessionID)
    if (!state) return false
    if (mode === "shadow") {
      const wouldDispatch =
        !state.dispatching &&
        state.pending &&
        state.attempt < MAX_IDLE_CONTINUE_ATTEMPTS &&
        state.dispatchedGeneration !== state.generation &&
        ALLOWED_AGENTS.has(state.agent) &&
        Boolean(options.client)
      log("smell-idle-continue shadow decision", {
        sessionID,
        wouldDispatch,
        attempt: state.attempt,
        generation: state.generation,
        category: state.failureCategory,
      })
      return false // shadow never calls promptAsync
    }
    if (!options.client) return false
    if (!ALLOWED_AGENTS.has(state.agent)) return false
    if (!state.pending) return false
    if (state.dispatching) return false
    if (state.attempt >= MAX_IDLE_CONTINUE_ATTEMPTS) return false
    if (state.dispatchedGeneration === state.generation) return false

    // Atomically mark dispatching for this generation before the async call.
    state.dispatching = true
    state.dispatchedGeneration = state.generation
    state.updatedAt = Date.now()

    const message = buildContinuationMessage(state)
    const dispatch = options.client!.session.promptAsync({
      path: { id: sessionID },
      query: { directory: state.directory },
      body: {
        agent: state.agent,
        parts: [{ type: "text", text: message }],
      },
    })
    Promise.resolve(dispatch)
      .then((res) => {
        const err = (res as { error?: unknown })?.error
        if (err) throw err
        state.attempt += 1
        state.pending = false
        state.dispatching = false
        state.updatedAt = Date.now()
        lastDispatchError = ""
        log("smell-idle-continue dispatched", {
          sessionID,
          attempt: state.attempt,
          generation: state.generation,
        })
      })
      .catch((error) => {
        state.dispatching = false
        state.updatedAt = Date.now()
        lastDispatchError = error instanceof Error ? error.message : String(error)
        log("smell-idle-continue dispatch failed", {
          sessionID,
          error: lastDispatchError,
        })
        // Do not auto-retry; stop for this generation.
      })
    return true
  }

  // Returns true when a real user message was detected (state cleared).
  function handleChatMessage(sessionID: string, parts: ReadonlyArray<{ type?: string; text?: unknown }>): boolean {
    if (!sessionID) return false
    const partList = Array.isArray(parts) ? parts : []
    // No parts at all → not a real message.
    if (partList.length === 0) return false
    const text = partList
      .map((p) => (p && p.type === "text" && typeof p.text === "string" ? p.text : ""))
      .join("\n")
      .trim()
    // If the message has parts but no text (e.g. pure file attachment), it is
    // still a real user message that should reset the continuation state.
    if (!text) {
      clearSession(sessionID)
      return true
    }
    if (text.startsWith(SMELL_IDLE_CONTINUE_PREFIX)) {
      // Our own injected continuation message: do not reset.
      return false
    }
    clearSession(sessionID)
    return true
  }

  return {
    recordFromBridgeOutput,
    handleIdle,
    handleChatMessage,
    handleSessionDeleted: clearSession,
    clearSession,
    clearAll,
    cleanupStale,
    dispose,
    peek,
    isDisposed: () => disposed,
    getLastDispatchError: () => lastDispatchError,
    size: () => states.size,
  }
}

export const SmellPlugin: Plugin = async ({ worktree, client }) => {
  const idleRuntime = createIdleContinueRuntime({ client })
  const commonShape = {
    projectRoot: tool.schema.string().describe("Absolute path to the source project root."),
    language: tool.schema
      .enum(["java"])
      .optional()
      .describe("Optional source language. Omit to infer from the target file extension."),
    smell: tool.schema.string().describe("Smell type, for example feature_envy or long_method."),
    location: tool.schema.string().describe("Location string, for example src/main/java/Foo.java:88."),
    config: tool.schema.string().optional().describe("Optional refactor.yaml path."),
    projects: tool.schema.string().optional().describe("Optional projects.yaml path."),
    smellEvidence: tool.schema.string().optional().describe("Optional per-sample smell evidence."),
    guardContextJson: tool.schema.string().optional().describe("Optional JSON object with extra guard context."),
  }
  const ideaShape = {
    projectRoot: tool.schema.string().optional().describe("Source project root. Defaults to SMELL_PROJECT_ROOT when set."),
    ideaProjectRoot: tool.schema
      .string()
      .optional()
      .describe("IDEA project root when different from the source project root. Defaults to SMELL_IDEA_PROJECT_ROOT when set."),
    ideaRefactorCli: tool.schema
      .string()
      .optional()
      .describe("IDEA refactor CLI path. Defaults to SMELL_IDEA_REFACTOR_CLI, IDEA_REFACTOR_CLI, or bundled bin/idea-refactor."),
  }
  const verifyTool = (name: string) =>
    tool({
      description: "Run smell verification and configured build/test. Failed results include failure_pack for narrow repair.",
      args: {
        ...commonShape,
        verificationMode: tool.schema
          .enum(["local", "auto", "sample_optimized", "project_full"])
          .optional()
          .describe("Verification mode. Defaults to local smell guard only; strict modes also run configured build/test."),
        noSnapshot: tool.schema.boolean().optional().describe("Do not include git status and source diff snapshot."),
        autoContinue: tool.schema
          .boolean()
          .optional()
          .describe("Allow the plugin to auto-inject one continuation message after a repairable idle failure (default false)."),
      },
      async execute(args, context) {
        const resolved = withBatchDefaults(args)
        const bridgeArgs = ["verify", ...commonArgs(resolved)]
        if (args.noSnapshot) bridgeArgs.push("--no-snapshot")
        const normalized = normalizeToolResult(name, await runBridge(worktree, bridgeArgs))
        // Update the in-process idle-continuation state machine from the bridge
        // output. This never throws into the tool result; it only attaches
        // metadata and arms continuation for a later session.idle dispatch.
        let autoContinuation: Record<string, unknown> | undefined
        try {
          const cont = idleRuntime.recordFromBridgeOutput({
            sessionID: context?.sessionID || "",
            agent: context?.agent || "",
            directory: context?.directory || "",
            taskKey: makeTaskKey(resolved.projectRoot || "", resolved.smell || "", resolved.location || ""),
            output: normalized.output,
            autoContinue: Boolean(args.autoContinue),
          })
          autoContinuation = {
            mode: cont.mode,
            enabled: cont.enabled,
            autoContinue: cont.autoContinue,
            attempt: cont.attempt,
            maxAttempts: cont.maxAttempts,
            generation: cont.generation,
            status: cont.status,
            category: cont.category,
            dispatched: cont.dispatched,
          }
        } catch {
          // State-machine bookkeeping must never break the verify tool result.
        }
        if (autoContinuation) {
          normalized.metadata.auto_continuation = toJsonSafe(autoContinuation)
        }
        return normalized
      },
    })

  return {
    tool: {
      smell_verify: verifyTool("Smell verification"),

      idea_refactor_locate: tool({
        description:
          "Locate an IDEA refactoring target. Successful locate replaces the current draft used by later prepare/apply.",
        args: {
          ...ideaShape,
          file: tool.schema.string().describe("Java file path, relative to the resolved IDEA project root, dataset root, or absolute."),
          line: tool.schema.number().int().describe("1-based caret line."),
          column: tool.schema.number().int().describe("1-based caret column."),
          selection: tool.schema
            .object({
              startLine: tool.schema.number().int().describe("1-based selection start line."),
              startColumn: tool.schema.number().int().describe("1-based selection start column."),
              endLine: tool.schema.number().int().describe("1-based selection end line."),
              endColumn: tool.schema.number().int().describe("1-based selection end column."),
            })
            .optional()
            .describe("Optional explicit selection range."),
          suggestSelectionsFor: tool.schema.string().optional().describe("Optional operation to request selection candidates for, for example extract:method."),
          expectedOperation: tool.schema.string().optional().describe("Optional operation expected in availableOperations."),
        },
        async execute(args) {
          const resolved = resolveIdeaInput(args)
          if (!resolved.ok) return resolved.result
          const resolvedFile = resolveIdeaFile(args.file, resolved.projectRoot)
          if (!resolvedFile.ok) return resolvedFile.result
          const cliArgs = [
            "locate",
            "--project-root",
            resolved.projectRoot,
            "--file",
            resolvedFile.file,
            "--line",
            String(args.line),
            "--column",
            String(args.column),
          ]
          addNumber(cliArgs, "--selection-start-line", args.selection?.startLine)
          addNumber(cliArgs, "--selection-start-column", args.selection?.startColumn)
          addNumber(cliArgs, "--selection-end-line", args.selection?.endLine)
          addNumber(cliArgs, "--selection-end-column", args.selection?.endColumn)
          addOptional(cliArgs, "--suggest-selections-for", args.suggestSelectionsFor)
          return renderIdeaResult(
            "IDEA refactor locate",
            await runIdeaCli(worktree, resolved.ideaRefactorCli, cliArgs),
            args.expectedOperation,
            { ...resolved.wrapperMetadata, ...ideaRuntimeMetadata(worktree, resolved.projectRoot) },
          )
        },
      }),

      idea_refactor_prepare: tool({
        description:
          "Prepare an IDEA refactoring operation against the current draft. Call idea_refactor_locate first.",
        args: {
          ...ideaShape,
          operation: tool.schema.string().describe("IDEA refactoring operation, for example extract:method or replace:method."),
          arguments: jsonObjectShape("Structured operation arguments. The wrapper serializes this to JSON safely."),
          decisions: jsonObjectShape("Structured decisions when IDEA requests a decision."),
          expectedOperation: tool.schema.string().optional().describe("Optional operation expected in the prepare payload."),
        },
        async execute(args) {
          const resolved = resolveIdeaInput(args)
          if (!resolved.ok) return resolved.result
          const cliArgs = ["prepare", "--project-root", resolved.projectRoot, "--operation", args.operation]
          addJson(cliArgs, "--arguments-json", args.arguments)
          addJson(cliArgs, "--decisions-json", args.decisions)
          return renderIdeaResult(
            "IDEA refactor prepare",
            await runIdeaCli(worktree, resolved.ideaRefactorCli, cliArgs),
            args.expectedOperation,
            { ...resolved.wrapperMetadata, ...ideaRuntimeMetadata(worktree, resolved.projectRoot) },
          )
        },
      }),

      idea_refactor_apply: tool({
        description:
          "Apply the current prepared IDEA refactoring draft. Call idea_refactor_prepare successfully first.",
        args: {
          ...ideaShape,
          arguments: jsonObjectShape("Structured operation arguments. The wrapper serializes this to JSON safely."),
          decisions: jsonObjectShape("Structured decisions when IDEA requests a decision."),
        },
        async execute(args) {
          const resolved = resolveIdeaInput(args)
          if (!resolved.ok) return resolved.result
          const cliArgs = ["apply", "--project-root", resolved.projectRoot]
          addJson(cliArgs, "--arguments-json", args.arguments)
          addJson(cliArgs, "--decisions-json", args.decisions)
          return renderIdeaResult(
            "IDEA refactor apply",
            await runIdeaCli(worktree, resolved.ideaRefactorCli, cliArgs),
            undefined,
            { ...resolved.wrapperMetadata, ...ideaRuntimeMetadata(worktree, resolved.projectRoot) },
          )
        },
      }),

      idea_edit: tool({
        description:
          "Apply an IDEA-backed oldString/newString source edit. Use for Java source patches instead of PSI structural insert/replace/delete operations.",
        args: {
          ...ideaShape,
          file: tool.schema.string().describe("Java file path, relative to the resolved IDEA project root, dataset root, or absolute."),
          oldString: tool.schema
            .string()
            .describe('Exact source block to replace. Must be unique unless replaceAll is true. Use "" only for explicit new-file or whole-file replacement steps.'),
          newString: tool.schema.string().describe("Replacement source block."),
          replaceAll: tool.schema.boolean().optional().describe("Replace every exact occurrence. Do not use for ordinary Java source patches."),
        },
        async execute(args) {
          const resolved = resolveIdeaInput(args)
          if (!resolved.ok) return resolved.result
          const resolvedFile = resolveIdeaFile(args.file, resolved.projectRoot)
          if (!resolvedFile.ok && String(args.oldString ?? "") !== "") return resolvedFile.result
          const cliArgs = [
            "edit",
            "--project-root",
            resolved.projectRoot,
            "--file",
            resolvedFile.ok ? resolvedFile.file : String(args.file),
            "--old-string",
            args.oldString,
            "--new-string",
            args.newString,
          ]
          if (args.replaceAll) cliArgs.push("--replace-all")
          return renderIdeaResult(
            "IDEA edit",
            await runIdeaCli(worktree, resolved.ideaRefactorCli, cliArgs),
            undefined,
            {
              ...resolved.wrapperMetadata,
              ...ideaRuntimeMetadata(worktree, resolved.projectRoot),
              postEditProblems:
                "Inspect payload.postEditProblems when present. New local IDEA problems are repair evidence; smell_verify remains the acceptance gate.",
            },
          )
        },
      }),

      idea_refactor_revert_last_apply: tool({
        description:
          "Revert the most recent successful IDEA apply. This is not for discarding the current locate/prepare draft.",
        args: {
          ...ideaShape,
        },
        async execute(args) {
          const resolved = resolveIdeaInput(args)
          if (!resolved.ok) return resolved.result
          const cliArgs = ["rollback", "--project-root", resolved.projectRoot]
          return renderIdeaResult(
            "IDEA refactor revert last apply",
            await runIdeaCli(worktree, resolved.ideaRefactorCli, cliArgs),
            undefined,
            {
              ...resolved.wrapperMetadata,
              ...ideaRuntimeMetadata(worktree, resolved.projectRoot),
              rollback_scope: "last_applied",
              warning: "This reverted a previously applied source change, not merely the current locate/prepare draft.",
            },
          )
        },
      }),
    },

    "tool.execute.before": async (input, output) => {
      if (input.tool !== "bash") return
      const command = String(output.args?.command ?? "")
      if (!command) return
      const rewritesJava =
        /\.java\b/.test(command) &&
        /\b(sed\s+-i|perl\s+-i|python3?\s+.*(write_text|open\(.+,.*w)|cat\s*>|tee\s+)/.test(command)
      if (rewritesJava) {
        throw new Error("Java source rewrites should use IDEA-Refactoring CLI or OpenCode edit tools, not shell text rewriting.")
      }
    },

    event: async ({ event }) => {
      try {
        if (!event || typeof event.type !== "string") return
        if (event.type === "session.idle") {
          const sessionID = (event as { properties?: { sessionID?: string } }).properties?.sessionID
          if (typeof sessionID === "string" && sessionID) {
            idleRuntime.handleIdle(sessionID)
          }
          return
        }
        if (event.type === "session.deleted") {
          const sessionID = (event as { properties?: { info?: { id?: string } } }).properties?.info?.id
          if (typeof sessionID === "string" && sessionID) {
            idleRuntime.handleSessionDeleted(sessionID)
          }
          return
        }
      } catch (error) {
        // Hooks must never throw into the event dispatcher; swallow and log.
        // eslint-disable-next-line no-console
        console.error("[smell] event hook error:", error instanceof Error ? error.message : String(error))
      }
    },

    "chat.message": async (input, output) => {
      try {
        const sessionID = input?.sessionID
        if (typeof sessionID !== "string" || !sessionID) return
        idleRuntime.handleChatMessage(sessionID, (output?.parts || []) as ReadonlyArray<{ type?: string; text?: unknown }>)
      } catch (error) {
        // eslint-disable-next-line no-console
        console.error("[smell] chat.message hook error:", error instanceof Error ? error.message : String(error))
      }
    },

    dispose: async () => {
      idleRuntime.dispose()
    },
  }
}

// opencode's plugin loader iterates Object.values(module) and requires every
// export to resolve (via lk) to a function or a { server } PluginModule. A plain
// object export like __smellSelfTest would make the loader throw "Plugin export
// is not a function". So the self-test helpers are attached as a property of the
// plugin function itself — the module then only exports functions (SmellPlugin +
// default, same reference) and the harness reads SmellPlugin.__selfTest.
;(SmellPlugin as Plugin & { __selfTest: unknown }).__selfTest = {
  normalizeToolResult,
  buildBridgeOutputPayload,
  normalizeMetadata,
  normalizeStdioFields,
  safeStringOutput,
  truncateText,
  safeJsonStringify,
  toJsonSafe,
  renderIdeaResult,
  MAX_STDOUT_STDERR_LEN,
  // Idle continuation pure helpers + constants (no production control surface):
  idleContinueMode,
  isOpendcodeRunMode,
  isBatchEnvironment,
  classifyFailureForContinue,
  makeTaskKey,
  buildContinuationMessage,
  redactSecrets,
  artifactPathsFrom,
  createIdleContinueRuntime,
  SMELL_IDLE_CONTINUE_PREFIX,
  MAX_IDLE_CONTINUE_ATTEMPTS,
  IDLE_CONTINUE_STATE_TTL_MS,
  ALLOWED_AGENTS,
  REPAIRABLE_CATEGORIES,
}

export default SmellPlugin

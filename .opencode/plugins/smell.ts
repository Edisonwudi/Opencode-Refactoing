import { spawn } from "node:child_process"
import { createHash } from "node:crypto"
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

type LoopPolicy = {
  mode: "off" | "verify-failure"
  max_continuations: number
  no_progress_limit: number
  allowed_failure_groups: string[]
  instruction: string
  sample_deadline_seconds: number
}

type CommandPolicy = {
  task: string
  verification_mode: "local" | "auto" | "sample_optimized" | "project_full"
  loop: LoopPolicy
}

type CommandLoopState = {
  policy: CommandPolicy
  startedAt: number
  continuationCount: number
  capRecoveryUsed: boolean
  noProgressCount: number
  lastFailureFingerprint: string
}

const COMMAND_LOOP_STATE_VERSION = 1
const COMMAND_LOOP_STATE_ENV = "SMELL_COMMAND_LOOP_STATE_JSON"

const pluginFile = fileURLToPath(import.meta.url)
const pluginRoot = path.resolve(path.dirname(pluginFile), "..", "..")
const bridgeFile = path.resolve(
  process.env.SMELL_BRIDGE_FILE || path.join(pluginRoot, "runtime", "python", "bridge", "smell_bridge.py"),
)
const bundledIdeaRefactorCli = path.resolve(pluginRoot, "bin", "idea-refactor")
const checkpointSmells = new Set([
  "long_method",
  "nested_complexity",
  "long_parameter_list",
  "feature_envy",
  "data_clumps",
  "code_clone_type1",
  "god_class",
  "refused_bequest",
  "switch_statements",
  "mysterious_name",
  "dead_code",
])
const checkpointObjectiveHints: Record<string, string> = {
  long_method: "Reduce the AST-NCSS of the named target method itself; moving or renaming unrelated code does not count.",
  nested_complexity: "Reduce the cognitive complexity of the named target method itself; cosmetic edits do not count.",
  long_parameter_list: "Reduce the parameter count of the named target declaration itself; changing only callers or another overload does not count. Migrate source callers and keep tests compiling, but do not retain the original long-signature compatibility overload and never edit tests.",
  feature_envy: "Reduce accesses from the target method to the expected envied receiver.",
  data_clumps: "Reduce the number of occurrences of the labeled parameter or field group.",
  code_clone_type1: "Reduce the exact-clone token count between the two labeled targets.",
  god_class: "Reduce at least one labeled class objective (NOM, NOF, WMC, LOC, or ATFD) without worsening behavior.",
  refused_bequest: "Reduce the labeled refusal score, suspicious overrides, or target rejection signals while preserving the inheritance contract.",
  switch_statements: "Reduce the case count or case density in the named target method; changing an unrelated switch does not count.",
  mysterious_name: "Remove the exact labeled suspicious identifier by giving it a meaningful name and updating its usages; unrelated renames do not count.",
  dead_code: "Remove the exact labeled dead declaration safely; editing unrelated declarations does not count.",
}

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

function taskField(task: string, label: string): string | undefined {
  const prefix = `${label.toLowerCase()}:`
  for (const rawLine of String(task || "").split(/\r?\n/)) {
    const line = rawLine.trim()
    if (line.toLowerCase().startsWith(prefix)) {
      const value = line.slice(prefix.length).trim()
      return value || undefined
    }
  }
  return undefined
}

function commandTaskIdentity(task: string) {
  return withBatchDefaults({
    projectRoot: taskField(task, "Project root"),
    language: taskField(task, "Language"),
    smell: taskField(task, "Smell type"),
    location: taskField(task, "Target location"),
    smellEvidence: taskField(task, "Smell evidence"),
  })
}

const MAX_STDOUT_STDERR_LEN = 4000

// --- session.idle command-policy continuation -------------------------------
//
// smell_verify attaches the authoritative command-owned loop decision and this
// runtime resumes the same session after session.idle when that decision is
// `continue`. There is no interactive/batch/run-mode switch and no second retry
// budget: command policy is the single source of truth.

const SMELL_IDLE_CONTINUE_PREFIX = "[smell-auto-continue"
const IDLE_CONTINUE_STATE_TTL_MS = 30 * 60 * 1000
const DIRECT_BUILD_COMMAND_RE =
  /(?:^|[;&|]\s*)(?:[A-Za-z_][A-Za-z0-9_]*=[^\s]+\s+)*(?:\.\/)?(?:mvnw|mvn|gradlew|gradle)\b/
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
  "STRUCTURAL_ROUTE_MISMATCH",
  "BUILD_COMPILE_ERROR",
  "TEST_BEHAVIOR_REGRESSION",
  "TEST_REFLECTION_ENTRY_STALE",
  "SAMPLE_TEST_FAILED",
])

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
  continuation: number
  maxContinuations: number
  instruction: string
  pending: boolean
  dispatching: boolean
  awaitingVerify: boolean
  awaitingVerifyReason: "initial" | "continuation"
  verifyReminderGeneration: number
  agent: string
  directory: string
  failureCategory: string
  verifyStatus: string
  failureHighlights: string[]
  artifactPaths: string[]
  updatedAt: number
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
  const lines: string[] = []
  lines.push(`${SMELL_IDLE_CONTINUE_PREFIX} ${state.continuation}/${state.maxContinuations}]`)
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
  lines.push(state.instruction || "Read the latest failure_pack and make one narrow corrective edit.")
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

function buildVerifyRequiredMessage(state: ContinuationState): string {
  const reason = state.awaitingVerifyReason === "continuation"
    ? "The previous corrective continuation ended without a new smell_verify result."
    : "No smell_verify call has completed for this refactoring task."
  return [
    `${SMELL_IDLE_CONTINUE_PREFIX} verify-required/${state.generation}]`,
    "",
    reason,
    "Call smell_verify now on the current production-Java changes.",
    "Treat its loop.decision as authoritative: continue only when instructed, otherwise stop.",
    "Do not modify or weaken tests.",
  ].join("\n")
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

// Create an isolated idle-continuation runtime. It consumes the authoritative
// loop decision already attached by applyCommandLoopDecision; it neither owns
// a second budget nor varies behavior by OpenCode invocation mode.
function createIdleContinueRuntime(options: {
  client?: { session: { promptAsync: (opts: unknown) => Promise<unknown> } }
  log?: (msg: string, details?: unknown) => void
}) {
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

  function armInitialVerification(input: {
    sessionID: string
    agent: string
    directory: string
    maxContinuations: number
    instruction: string
  }) {
    if (!input.sessionID) return
    states.set(input.sessionID, {
      taskKey: "",
      generation: 0,
      dispatchedGeneration: -1,
      continuation: 0,
      maxContinuations: input.maxContinuations,
      instruction: input.instruction,
      pending: false,
      dispatching: false,
      awaitingVerify: true,
      awaitingVerifyReason: "initial",
      verifyReminderGeneration: -1,
      agent: input.agent,
      directory: input.directory,
      failureCategory: "",
      verifyStatus: "",
      failureHighlights: [],
      artifactPaths: [],
      updatedAt: Date.now(),
    })
  }

  // Called from smell_verify.execute after a bridge result is normalized.
  // Returns the auto_continuation metadata to attach to the tool result.
  function recordFromBridgeOutput(input: {
    sessionID: string
    agent: string
    directory: string
    taskKey: string
    output: string
  }): {
    enabled: boolean
    continuation: number
    maxContinuations: number
    generation: number
    status: string
    category: string
    dispatched: boolean
  } {
    cleanupStale()
    let status = ""
    let category = ""
    let jsonParsed = false
    let failurePack: unknown = null
    let loop: Record<string, unknown> | null = null
    try {
      const parsed = JSON.parse(input.output) as Record<string, unknown>
      jsonParsed = true
      status = typeof parsed.status === "string" ? parsed.status : ""
      category = typeof parsed.failure_category === "string" ? parsed.failure_category : ""
      failurePack = parsed.failure_pack
      loop = parsed.loop && typeof parsed.loop === "object" && !Array.isArray(parsed.loop)
        ? parsed.loop as Record<string, unknown>
        : null
    } catch {
      // Non-JSON bridge output: never continue.
    }

    const existing = states.get(input.sessionID)
    if (existing) {
      // Reaching this function proves that smell_verify completed. Re-arm only
      // after a continuation is actually dispatched.
      existing.awaitingVerify = false
      existing.updatedAt = Date.now()
    }
    const continuation = typeof loop?.continuation === "number" ? loop.continuation : 0
    const maxContinuations = typeof loop?.max_continuations === "number" ? loop.max_continuations : 0
    const decision = typeof loop?.decision === "string" ? loop.decision : "stop"
    const instruction = typeof loop?.instruction === "string" ? loop.instruction : ""

    const base = {
      enabled: true,
      continuation,
      maxContinuations,
      generation: existing ? existing.generation : 0,
      status,
      category: category || (existing ? existing.failureCategory : ""),
      dispatched: existing ? existing.dispatchedGeneration === existing.generation : false,
    }

    // Any result other than the controller's authoritative `continue` revokes a
    // stale pending generation. This includes PASS, malformed output, disabled
    // policy, non-repairable failure, deadline, no-progress, and exhausted cap.
    const revokePending = () => {
      if (existing) {
        existing.pending = false
        existing.dispatching = false
        existing.awaitingVerify = false
        existing.updatedAt = Date.now()
      }
    }

    if (!jsonParsed || decision !== "continue" || continuation <= 0 || continuation > maxContinuations) {
      revokePending()
      return { ...base, dispatched: false }
    }

    const classification = classifyFailureForContinue(failurePack)
    // applyCommandLoopDecision already validated repairability and consumed one
    // unit from the shared command-policy budget.
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
          continuation,
          maxContinuations,
          instruction,
          pending: true,
          dispatching: false,
          awaitingVerify: false,
          awaitingVerifyReason: "continuation",
          verifyReminderGeneration: -1,
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
      nextState.continuation = continuation
      nextState.maxContinuations = maxContinuations
      nextState.instruction = instruction
      nextState.pending = true
      nextState.awaitingVerify = false
      nextState.awaitingVerifyReason = "continuation"
      nextState.verifyReminderGeneration = -1
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
      continuation: nextState.continuation,
      maxContinuations: nextState.maxContinuations,
    })
    return {
      ...base,
      continuation: nextState.continuation,
      maxContinuations: nextState.maxContinuations,
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
    const state = states.get(sessionID)
    if (!state) return false
    if (!options.client) return false
    if (state.dispatching) return false

    if (!state.pending) {
      if (!state.awaitingVerify) return false
      if (state.verifyReminderGeneration === state.generation) return false

      state.dispatching = true
      state.verifyReminderGeneration = state.generation
      state.updatedAt = Date.now()
      const message = buildVerifyRequiredMessage(state)
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
          state.dispatching = false
          state.updatedAt = Date.now()
          lastDispatchError = ""
          log("smell-verify-required dispatched", {
            sessionID,
            generation: state.generation,
            reason: state.awaitingVerifyReason,
          })
        })
        .catch((error) => {
          state.dispatching = false
          state.updatedAt = Date.now()
          lastDispatchError = error instanceof Error ? error.message : String(error)
          log("smell-verify-required dispatch failed", { sessionID, error: lastDispatchError })
        })
      return true
    }

    if (state.continuation <= 0 || state.continuation > state.maxContinuations) return false
    if (state.dispatchedGeneration === state.generation) return false

    // Atomically mark dispatching for this generation before the async call.
    state.dispatching = true
    state.dispatchedGeneration = state.generation
    state.updatedAt = Date.now()
    const dispatchedGeneration = state.generation

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
        // A verify call may finish while promptAsync is resolving. Only arm
        // the reminder if this is still the generation we dispatched.
        if (state.generation === dispatchedGeneration) {
          state.pending = false
          state.dispatching = false
          state.awaitingVerify = true
          state.awaitingVerifyReason = "continuation"
          state.verifyReminderGeneration = -1
          state.updatedAt = Date.now()
        }
        lastDispatchError = ""
        log("smell-idle-continue dispatched", {
          sessionID,
          continuation: state.continuation,
          maxContinuations: state.maxContinuations,
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
    armInitialVerification,
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

function parseCommandPolicyResult(result: BridgeResult): CommandPolicy {
  if (result.exitCode !== 0 || !result.json || typeof result.json !== "object" || Array.isArray(result.json)) {
    const parsed = result.json as { error?: unknown } | null
    const detail = typeof parsed?.error === "string" ? parsed.error : (result.stderr || "command policy could not be resolved")
    throw new Error(detail)
  }
  const payload = result.json as Record<string, unknown>
  const loop = payload.loop
  if (!loop || typeof loop !== "object" || Array.isArray(loop)) {
    throw new Error("INVALID_LOOP_POLICY: resolver returned no loop policy")
  }
  return payload as unknown as CommandPolicy
}

function commandPolicyPrompt(policy: CommandPolicy): string {
  const allowed = policy.loop.allowed_failure_groups.join(", ") || "none"
  const lines = [
    policy.task,
    "",
    "Controller-owned verification and loop policy:",
    `- verification_mode: ${policy.verification_mode}`,
    `- loop_mode: ${policy.loop.mode}`,
    `- max_continuations: ${policy.loop.max_continuations}`,
    `- no_progress_limit: ${policy.loop.no_progress_limit}`,
    `- allowed_failure_groups: ${allowed}`,
    `- sample_deadline_seconds: ${policy.loop.sample_deadline_seconds}`,
    `- continuation_instruction: ${policy.loop.instruction}`,
    "",
    "Call smell_verify as the acceptance gate. Its loop.decision field is authoritative.",
    "When loop.decision is continue, follow loop.instruction and call smell_verify again.",
    "When loop.decision is stop, stop and report loop.termination_reason. Never modify or weaken tests.",
  ]
  const smell = String(taskField(policy.task, "Smell type") || "")
  const evidence = String(taskField(policy.task, "Smell evidence") || "")
  if (
    smell === "refused_bequest"
    && /(?:^|;)\s*structural_expectation\s*=\s*capability_split(?:\s*;|$)/i.test(evidence)
  ) {
    lines.push(
      "",
      "Mandatory Refused Bequest route lock:",
      "- required_route: capability_split",
      "- A body-only implementation or delegation of the reported method cannot pass, even when build and behavior tests pass.",
      "- Before the first edit, inspect the parent contract, relevant sibling implementers, and production callers.",
      "- Split the parent into narrow supported capabilities, migrate implementers and callers, and remove the unsupported operation from the refusing type's inherited contract.",
      "- Do not relocate the rejecting/empty/null behavior to an ancestor, interface default, adapter, or compatibility shim.",
    )
  }
  if (checkpointSmells.has(smell)) {
    lines.push(
      "",
      "Continuous-metric checkpoint contract:",
      "- The dataset label is authoritative even when the strict threshold detector is initially clean.",
      "- An unchanged baseline can never pass; make a substantive production-Java refactoring.",
      "- Acceptance requires at least one adapter objective to decrease plus the ordinary smell guard and build/test preservation.",
      `- Adapter objective: ${checkpointObjectiveHints[smell]}`,
    )
  }
  return lines.join("\n")
}

function capabilityPlanPrompt(payload: Record<string, unknown>): string {
  const impactMap = payload.capability_impact_map
  if (!impactMap || typeof impactMap !== "object" || Array.isArray(impactMap)) {
    throw new Error("CAPABILITY_PLAN_FAILED: build-plan-context returned no capability_impact_map")
  }
  const impact = impactMap as Record<string, unknown>
  if (impact.ok !== true) {
    throw new Error(
      `CAPABILITY_PLAN_FAILED: ${String(impact.error || "capability impact map could not be resolved")}`,
    )
  }
  return [
    "",
    "Pre-edit capability impact map (generated from the production semantic model):",
    safeJsonStringify(impact),
    "",
    "Use this as a closure worklist before the first edit. Inspect every declaration, implementer,",
    "production call site, and inherited_surface_at_risk entry. If changing a superclass, preserve",
    "or explicitly migrate its non-target state and API. Manually resolve every receiver marked",
    "unresolved; do not skip it.",
  ].join("\n")
}

function defaultCommandPolicy(
  verificationMode: CommandPolicy["verification_mode"] = "local",
): CommandPolicy {
  return {
    task: "Complete the current smell refactoring task.",
    verification_mode: verificationMode,
    loop: {
      mode: "verify-failure",
      max_continuations: 2,
      no_progress_limit: 1,
      allowed_failure_groups: ["smell", "compile", "test"],
      instruction: "Read the latest failure_pack, make one narrow corrective edit, and call smell_verify again. Do not modify or weaken tests.",
      sample_deadline_seconds: 1800,
    },
  }
}

function newCommandLoopState(policy: CommandPolicy): CommandLoopState {
  return {
    policy,
    startedAt: Date.now(),
    continuationCount: 0,
    capRecoveryUsed: false,
    noProgressCount: 0,
    lastFailureFingerprint: "",
  }
}

function commandLoopStateSnapshot(state: CommandLoopState): Record<string, unknown> {
  return {
    schema_version: COMMAND_LOOP_STATE_VERSION,
    policy: {
      ...state.policy,
      // The original task can contain a large group manifest. It is not used
      // after command initialization, so do not duplicate it in every tool
      // event or runner handoff.
      task: "Continue the current smell refactoring task.",
    },
    started_at: state.startedAt,
    continuation_count: state.continuationCount,
    cap_recovery_used: state.capRecoveryUsed,
    no_progress_count: state.noProgressCount,
    last_failure_fingerprint: state.lastFailureFingerprint,
  }
}

function restoreCommandLoopState(raw: string | undefined): CommandLoopState | undefined {
  if (!raw) return undefined
  try {
    const parsed = JSON.parse(raw) as Record<string, unknown>
    if (parsed.schema_version !== COMMAND_LOOP_STATE_VERSION) return undefined
    const policy = parsed.policy as CommandPolicy | undefined
    const loop = policy?.loop
    if (
      !policy
      || !["local", "auto", "sample_optimized", "project_full"].includes(policy.verification_mode)
      || !loop
      || !["off", "verify-failure"].includes(loop.mode)
      || !Number.isInteger(loop.max_continuations)
      || loop.max_continuations < 0
      || loop.max_continuations > 5
      || !Number.isInteger(loop.no_progress_limit)
      || loop.no_progress_limit < 0
      || !Array.isArray(loop.allowed_failure_groups)
      || !loop.allowed_failure_groups.every((item) => typeof item === "string")
      || typeof loop.instruction !== "string"
      || !Number.isFinite(loop.sample_deadline_seconds)
      || loop.sample_deadline_seconds <= 0
    ) return undefined
    const startedAt = Number(parsed.started_at)
    const continuationCount = Number(parsed.continuation_count)
    const noProgressCount = Number(parsed.no_progress_count)
    if (
      !Number.isFinite(startedAt)
      || !Number.isInteger(continuationCount)
      || continuationCount < 0
      || continuationCount > loop.max_continuations
      || !Number.isInteger(noProgressCount)
      || noProgressCount < 0
    ) return undefined
    return {
      policy,
      startedAt,
      continuationCount,
      capRecoveryUsed: parsed.cap_recovery_used === true,
      noProgressCount,
      lastFailureFingerprint:
        typeof parsed.last_failure_fingerprint === "string"
          ? parsed.last_failure_fingerprint
          : "",
    }
  } catch {
    return undefined
  }
}

function hasActionableProgressAtCap(payload: Record<string, unknown>, failureGroup: string): boolean {
  const checkpoint = payload.checkpoint && typeof payload.checkpoint === "object" && !Array.isArray(payload.checkpoint)
    ? payload.checkpoint as Record<string, unknown>
    : undefined
  const delta = checkpoint?.delta && typeof checkpoint.delta === "object" && !Array.isArray(checkpoint.delta)
    ? checkpoint.delta as Record<string, unknown>
    : undefined
  if (delta?.metric_progress === true) {
    return true
  }
  if (failureGroup !== "compile" && failureGroup !== "test") {
    return false
  }
  const snapshot = payload.snapshot && typeof payload.snapshot === "object" && !Array.isArray(payload.snapshot)
    ? payload.snapshot as Record<string, unknown>
    : undefined
  const diffStat = snapshot?.diff_stat && typeof snapshot.diff_stat === "object" && !Array.isArray(snapshot.diff_stat)
    ? snapshot.diff_stat as Record<string, unknown>
    : undefined
  return delta?.has_production_diff === true
    || (typeof diffStat?.stdout === "string" && diffStat.stdout.trim().length > 0)
}

function failureFingerprint(payload: Record<string, unknown>): string {
  const pack = payload.failure_pack
  const smellGuard = payload.smell_guard && typeof payload.smell_guard === "object" && !Array.isArray(payload.smell_guard)
    ? payload.smell_guard as Record<string, unknown>
    : null
  const firstResult = Array.isArray(smellGuard?.results) && smellGuard!.results.length > 0
    && smellGuard!.results[0] && typeof smellGuard!.results[0] === "object"
    ? smellGuard!.results[0] as Record<string, unknown>
    : null
  const details = firstResult?.details && typeof firstResult.details === "object" && !Array.isArray(firstResult.details)
    ? firstResult.details as Record<string, unknown>
    : null
  const metricDelta = details?.metric_delta && typeof details.metric_delta === "object" && !Array.isArray(details.metric_delta)
    ? details.metric_delta as Record<string, unknown>
    : null
  const source = pack && typeof pack === "object" && !Array.isArray(pack)
    ? {
        category: (pack as Record<string, unknown>).failure_category || "",
        status: (pack as Record<string, unknown>).verify_status || payload.status || "",
        highlights: (pack as Record<string, unknown>).highlights || [],
        checkpointProgress: details ? {
          reason: details.reason || "",
          hasProductionDiff: details.has_production_diff === true,
          objectives: metricDelta?.objectives || null,
        } : null,
      }
    : { category: "", status: payload.status || "", highlights: [] }
  return createHash("sha256").update(JSON.stringify(source)).digest("hex")
}

function applyCommandLoopDecision(normalized: { output: string; metadata: Record<string, unknown> }, state: CommandLoopState) {
  let payload: Record<string, unknown>
  try {
    payload = JSON.parse(normalized.output) as Record<string, unknown>
  } catch {
    return
  }
  const passed = payload.success === true || payload.status === "PASS"
  const resolution = typeof payload.resolution === "string" ? payload.resolution : ""
  // An improved PASS accepts real progress (production diff + metric
  // reduction) but the detector still reports the smell. Keep the loop
  // running toward resolved within the same budget instead of terminating;
  // only resolution=resolved (or an exhausted budget) stops the session.
  const improvedOnly = passed && resolution === "improved"
  const checkpointObj = payload.checkpoint && typeof payload.checkpoint === "object" && !Array.isArray(payload.checkpoint)
    ? payload.checkpoint as Record<string, unknown>
    : undefined
  const bestPartial = checkpointObj?.best_partial && typeof checkpointObj.best_partial === "object" && !Array.isArray(checkpointObj.best_partial)
    ? checkpointObj.best_partial as Record<string, unknown>
    : undefined
  const pack = payload.failure_pack
  const category = pack && typeof pack === "object" && !Array.isArray(pack)
    ? String((pack as Record<string, unknown>).failure_category || "")
    : ""
  const group = pack && typeof pack === "object" && !Array.isArray(pack)
    ? String((pack as Record<string, unknown>).failure_group || "")
    : ""
  const bridgeRetryable = pack && typeof pack === "object" && !Array.isArray(pack)
    ? (pack as Record<string, unknown>).retryable === true
    : false
  const retryable = Boolean(bridgeRetryable && group && state.policy.loop.allowed_failure_groups.includes(group))
  const elapsedSeconds = Math.max(0, Math.floor((Date.now() - state.startedAt) / 1000))
  let decision: "continue" | "stop" = "stop"
  let terminationReason = "PASS"

  if (!passed || improvedOnly) {
    const fingerprint = improvedOnly
      ? "improved:" + JSON.stringify(bestPartial?.objectives ?? null)
      : failureFingerprint(payload)
    if (state.lastFailureFingerprint && state.lastFailureFingerprint === fingerprint) {
      state.noProgressCount += 1
    } else {
      state.noProgressCount = 0
    }
    state.lastFailureFingerprint = fingerprint

    if (state.policy.loop.mode === "off" || state.policy.loop.max_continuations <= 0) {
      terminationReason = improvedOnly ? "PASS" : "LOOP_DISABLED"
    } else if (!improvedOnly && !retryable) {
      terminationReason = "NON_REPAIRABLE_FAILURE"
    } else if (elapsedSeconds >= state.policy.loop.sample_deadline_seconds) {
      terminationReason = improvedOnly ? "PASS" : "SAMPLE_DEADLINE_REACHED"
    } else if (state.noProgressCount >= state.policy.loop.no_progress_limit) {
      terminationReason = improvedOnly ? "PASS" : "NO_PROGRESS"
    } else if (state.continuationCount >= state.policy.loop.max_continuations) {
      if (
        !improvedOnly
        && retryable
        && !state.capRecoveryUsed
        && hasActionableProgressAtCap(payload, group)
      ) {
        // One final, bounded repair is part of the shared command policy so
        // TUI/Web/serve/attach and batch runs receive identical behavior.
        // Keep the public continuation count at the configured maximum; the
        // separate flag prevents a second recovery.
        state.capRecoveryUsed = true
        decision = "continue"
        terminationReason = ""
      } else {
        terminationReason = improvedOnly ? "PASS" : "MAX_CONTINUATIONS_REACHED"
      }
    } else {
      state.continuationCount += 1
      decision = "continue"
      terminationReason = ""
    }
  }

  const loop = {
    decision,
    termination_reason: terminationReason,
    continuation: state.continuationCount,
    max_continuations: state.policy.loop.max_continuations,
    cap_recovery_used: state.capRecoveryUsed,
    remaining: Math.max(0, state.policy.loop.max_continuations - state.continuationCount),
    no_progress_count: state.noProgressCount,
    no_progress_limit: state.policy.loop.no_progress_limit,
    elapsed_seconds: elapsedSeconds,
    sample_deadline_seconds: state.policy.loop.sample_deadline_seconds,
    failure_category: category,
    failure_group: group,
    instruction: decision === "continue"
      ? (category === "STRUCTURAL_ROUTE_MISMATCH"
          ? "Capability split is mandatory. Revert or replace any body-only implementation of the reported method; split the parent capability, migrate real implementers and production callers to narrow types, remove the unsupported inherited operation, then call smell_verify again. Do not modify or weaken tests."
          : improvedOnly && typeof payload.continue_hint === "string" && payload.continue_hint
            ? payload.continue_hint
            : state.policy.loop.instruction)
      : "",
  }
  payload.loop = loop
  normalized.output = safeJsonStringify(payload)
  normalized.metadata.loop = toJsonSafe(loop)
}

export const SmellPlugin: Plugin = async ({ worktree, client }) => {
  const idleRuntime = createIdleContinueRuntime({ client })
  const commandLoopStates = new Map<string, CommandLoopState>()
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
      },
      async execute(args, context) {
        const resolved = withBatchDefaults(args)
        const sessionID = context?.sessionID || ""
        let commandState = commandLoopStates.get(sessionID)
        if (!commandState && sessionID) {
          const requestedMode = String(resolved.verificationMode || "local") as CommandPolicy["verification_mode"]
          commandState = restoreCommandLoopState(process.env[COMMAND_LOOP_STATE_ENV])
            || newCommandLoopState(defaultCommandPolicy(requestedMode))
          commandLoopStates.set(sessionID, commandState)
        }
        if (commandState) {
          resolved.verificationMode = commandState.policy.verification_mode
        }
        const bridgeArgs = ["verify", ...commonArgs(resolved)]
        if (args.noSnapshot) bridgeArgs.push("--no-snapshot")
        const normalized = normalizeToolResult(name, await runBridge(worktree, bridgeArgs))
        if (commandState) {
          applyCommandLoopDecision(normalized, commandState)
          normalized.metadata.command_loop_state = toJsonSafe(commandLoopStateSnapshot(commandState))
        }
        // Consume the authoritative loop decision and arm same-session
        // continuation. This path is identical for TUI, run, serve, web,
        // attach, and batch environments.
        let autoContinuation: Record<string, unknown> | undefined
        try {
          const cont = idleRuntime.recordFromBridgeOutput({
            sessionID,
            agent: context?.agent || "",
            directory: context?.directory || "",
            taskKey: makeTaskKey(resolved.projectRoot || "", resolved.smell || "", resolved.location || ""),
            output: normalized.output,
          })
          autoContinuation = {
            enabled: cont.enabled,
            continuation: cont.continuation,
            maxContinuations: cont.maxContinuations,
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

  const planTool = tool({
    description:
      "Build a read-only refactoring plan context. Capability-split Refused Bequest results include contract declarations, implementers, and production call sites.",
    args: {
      ...commonShape,
    },
    async execute(args) {
      const resolved = withBatchDefaults(args)
      const bridgeArgs = [
        "build-plan-context",
        ...commonArgs(resolved),
        "--no-idea-preflight",
        "--no-idea-open",
      ]
      return normalizeToolResult("Smell refactoring plan", await runBridge(worktree, bridgeArgs))
    },
  })

  return {
    tool: {
      smell_plan: planTool,
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
      const sessionID = typeof input.sessionID === "string" ? input.sessionID : ""
      if (sessionID && commandLoopStates.has(sessionID) && DIRECT_BUILD_COMMAND_RE.test(command)) {
        throw new Error(
          "Do not run Maven or Gradle directly during a smell-refactor command. "
          + "Call smell_verify now; it owns the pinned offline build/test command and loop decision.",
        )
      }
      const rewritesJava =
        /\.java\b/.test(command) &&
        /\b(sed\s+-i|perl\s+-i|python3?\s+.*(write_text|open\(.+,.*w)|cat\s*>|tee\s+)/.test(command)
      if (rewritesJava) {
        throw new Error("Java source rewrites should use IDEA-Refactoring CLI or OpenCode edit tools, not shell text rewriting.")
      }
    },

    "command.execute.before": async (input, output) => {
      if (
        input.command !== "smell-refactor-run" &&
        input.command !== "java-refactor-run" &&
        input.command !== "java-refactor-run-idea"
      ) return
      const result = await runBridge(worktree, ["resolve-command", "--arguments", input.arguments])
      const policy = parseCommandPolicyResult(result)
      const identity = commandTaskIdentity(policy.task)
      let capabilityPlan = ""
      if (
        identity.smell === "refused_bequest"
        && /(?:^|;)\s*structural_expectation\s*=\s*capability_split(?:\s*;|$)/i.test(
          String(identity.smellEvidence || ""),
        )
      ) {
        if (!identity.projectRoot || !identity.location) {
          throw new Error("CAPABILITY_PLAN_FAILED: command task identity is incomplete")
        }
        const planResult = await runBridge(worktree, [
          "build-plan-context",
          ...commonArgs({
            projectRoot: String(identity.projectRoot),
            projectOverrideRoot: identity.projectOverrideRoot,
            language: identity.language,
            smell: String(identity.smell),
            location: String(identity.location),
            smellEvidence: identity.smellEvidence,
          }),
          "--no-idea-preflight",
          "--no-idea-open",
        ])
        const planPayload = planResult.json as Record<string, unknown> | null
        if (planResult.exitCode !== 0 || !planPayload || planPayload.success !== true) {
          throw new Error(
            `CAPABILITY_PLAN_FAILED: ${truncateText(planResult.stderr || planResult.stdout)}`,
          )
        }
        capabilityPlan = capabilityPlanPrompt(planPayload)
      }
      if (identity.smell && checkpointSmells.has(identity.smell)) {
        if (!identity.projectRoot || !identity.location) {
          throw new Error("CHECKPOINT_BASELINE_CAPTURE_FAILED: command task identity is incomplete")
        }
        const baselineResult = await runBridge(worktree, [
          "capture-baseline",
          ...commonArgs({
            projectRoot: String(identity.projectRoot),
            projectOverrideRoot: identity.projectOverrideRoot,
            language: identity.language,
            smell: String(identity.smell),
            location: String(identity.location),
            smellEvidence: identity.smellEvidence,
          }),
        ])
        const baselinePayload = baselineResult.json as Record<string, unknown> | null
        if (baselineResult.exitCode !== 0 || !baselinePayload || baselinePayload.success !== true) {
          throw new Error(
            `CHECKPOINT_BASELINE_CAPTURE_FAILED: ${truncateText(baselineResult.stderr || baselineResult.stdout)}`,
          )
        }
      }
      commandLoopStates.set(input.sessionID, newCommandLoopState(policy))
      idleRuntime.clearSession(input.sessionID)
      idleRuntime.armInitialVerification({
        sessionID: input.sessionID,
        agent:
          input.command === "java-refactor-run-idea"
            ? "java-refactor-agent-idea"
            : input.command === "smell-refactor-run"
              ? "smell-refactor-agent"
              : "java-refactor-agent",
        directory: worktree,
        maxContinuations: policy.loop.max_continuations,
        instruction: policy.loop.instruction,
      })
      output.parts = [
        { type: "text", text: commandPolicyPrompt(policy) + capabilityPlan },
      ] as typeof output.parts
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
            commandLoopStates.delete(sessionID)
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
      commandLoopStates.clear()
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
  parseCommandPolicyResult,
  commandPolicyPrompt,
  defaultCommandPolicy,
  newCommandLoopState,
  commandLoopStateSnapshot,
  restoreCommandLoopState,
  failureFingerprint,
  applyCommandLoopDecision,
  MAX_STDOUT_STDERR_LEN,
  // Idle continuation pure helpers + constants (no production control surface):
  classifyFailureForContinue,
  makeTaskKey,
  buildContinuationMessage,
  buildVerifyRequiredMessage,
  redactSecrets,
  artifactPathsFrom,
  createIdleContinueRuntime,
  SMELL_IDLE_CONTINUE_PREFIX,
  IDLE_CONTINUE_STATE_TTL_MS,
  REPAIRABLE_CATEGORIES,
}

export default SmellPlugin

import { spawn } from "node:child_process"
import { createHash } from "node:crypto"
import { fileURLToPath } from "node:url"
import path from "node:path"
import { existsSync, readFileSync, readdirSync, writeFileSync } from "node:fs"
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

type IdeaDetail = "compact" | "full"

type IdeaSemanticTarget = {
  fqcn?: string
  memberName?: string
  parameterTypes?: string[]
  filePath?: string
  packageName?: string
  directoryPath?: string
  moduleName?: string
}

type IdeaSelection = {
  startLine: number
  startColumn: number
  endLine: number
  endColumn: number
}

type IdeaPreviewRequest = {
  projectRoot: string
  operation: string
  proposalId?: string
  target?: IdeaSemanticTarget
  file?: string
  line?: number
  column?: number
  selection?: IdeaSelection
  arguments?: Record<string, unknown>
  decisions?: Record<string, unknown>
  detail?: IdeaDetail
}

type IdeaCliRunner = (worktree: string, cli: string, args: string[]) => Promise<IdeaCliResult>

type LoopPolicy = {
  mode: "off" | "verify-failure"
  max_continuations: number
  no_progress_limit: number
  allowed_failure_groups: string[]
  instruction: string
  sample_deadline_seconds: number
}

type VerificationMode = "local" | "auto" | "sample_optimized" | "project_full"

type CommandTaskIdentity = {
  project_root: string
  project_override_root: string
  language: string
  smell: string
  location: string
  target_context_json: string
  verification_mode: VerificationMode
  sample_test_location: string
  sample_test_command: string
}

type CommandPolicy = {
  task: string
  verification_mode: VerificationMode
  allow_test_changes: boolean
  loop: LoopPolicy
  identity: CommandTaskIdentity
  checkpoint_required: boolean
}

type ControllerIdentity = {
  projectRoot: string
  projectOverrideRoot?: string
  language?: string
  smell: string
  location: string
  targetContextJson?: string
  verificationMode: VerificationMode
  sampleTestLocation?: string
  sampleTestCommand?: string
  checkpointRequired: boolean
}

type CommandIdentityBinding = {
  project_root: string
  project_override_root: string
  language: string
  smell: string
  location: string
  target_context_json: string
  verification_mode: string
  sample_test_location: string
  sample_test_command: string
}

type CommandLoopState = {
  policy: CommandPolicy
  targetIdentityContext: string
  startedAt: number
  continuationCount: number
  capRecoveryUsed: boolean
  noProgressCount: number
  lastFailureFingerprint: string
}

const COMMAND_LOOP_STATE_VERSION = 3
const COMMAND_LOOP_STATE_ENV = "SMELL_COMMAND_LOOP_STATE_JSON"
const BASELINE_CONTEXT_FILE_ENV = "SMELL_BASELINE_CONTEXT_FILE"
const CONTROLLER_CONTEXT_AUDIT_FILE_ENV = "SMELL_CONTROLLER_CONTEXT_AUDIT_FILE"

const pluginFile = fileURLToPath(import.meta.url)
const pluginRoot = path.resolve(path.dirname(pluginFile), "..", "..")
const bridgeFile = path.resolve(
  process.env.SMELL_BRIDGE_FILE || path.join(pluginRoot, "runtime", "python", "bridge", "smell_bridge.py"),
)
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

function ideaDecisionsShape(description: string) {
  return tool.schema
    .record(
      tool.schema.string(),
      tool.schema
        .object({
          choice: tool.schema.string(),
          arguments: tool.schema.record(tool.schema.string(), tool.schema.unknown()).optional(),
        })
        .strict(),
    )
    .optional()
    .describe(description)
}

function withBatchDefaults(input: {
  projectRoot?: string
  language?: string
  smell?: string
  location?: string
  targetContextJson?: string
  verificationMode?: string
  [key: string]: unknown
}) {
  const envProjectRoot = envDefault("SMELL_PROJECT_ROOT")
  const envCanonicalProjectRoot = envDefault("SMELL_CANONICAL_PROJECT_ROOT")
  const envLanguage = envDefault("SMELL_LANGUAGE")
  const envSmell = envDefault("SMELL_SMELL")
  const envLocation = envDefault("SMELL_LOCATION")
  const envTargetContext = envDefault("SMELL_TARGET_CONTEXT_JSON")
  const envVerificationMode = envDefault("SMELL_VERIFICATION_MODE")
  const envSampleTestLocation = envDefault("SMELL_SAMPLE_TEST_LOCATION")
  const envSampleTestCommand = envDefault("SMELL_SAMPLE_TEST_COMMAND")
  const hasBatchIdentity = Boolean(envProjectRoot && envSmell && envLocation)
  return {
    ...input,
    projectRoot: hasBatchIdentity ? envProjectRoot! : input.projectRoot,
    projectOverrideRoot: envCanonicalProjectRoot,
    language: hasBatchIdentity ? envLanguage : (input.language || envLanguage),
    smell: hasBatchIdentity ? envSmell! : input.smell,
    location: hasBatchIdentity ? envLocation! : input.location,
    targetContextJson: hasBatchIdentity ? envTargetContext : (input.targetContextJson || envTargetContext),
    verificationMode: hasBatchIdentity ? envVerificationMode : (input.verificationMode || envVerificationMode),
    sampleTestLocation: envSampleTestLocation,
    sampleTestCommand: envSampleTestCommand,
    checkpointRequired: input.checkpointRequired === true,
  }
}

function commonArgs(input: {
  projectRoot: string
  projectOverrideRoot?: string
  language?: string
  smell: string
  location: string
  baselineSeal?: string
  targetContextJson?: string
  allowTestChanges?: boolean
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
  addOptional(args, "--baseline-seal", input.baselineSeal)
  addOptional(args, "--target-context-json", input.targetContextJson)
  if (input.allowTestChanges) args.push("--allow-test-changes")
  addOptional(args, "--verification-mode", input.verificationMode)
  addOptional(args, "--sample-test-location", input.sampleTestLocation)
  addOptional(args, "--sample-test-command", input.sampleTestCommand)
  return args
}

function controllerIdentityFromPolicy(policy: CommandPolicy): ControllerIdentity {
  const identity = policy.identity
  return {
    projectRoot: identity.project_root,
    projectOverrideRoot: identity.project_override_root || undefined,
    language: identity.language || undefined,
    smell: identity.smell,
    location: identity.location,
    targetContextJson: identity.target_context_json || undefined,
    verificationMode: identity.verification_mode,
    sampleTestLocation: identity.sample_test_location || undefined,
    sampleTestCommand: identity.sample_test_command || undefined,
    checkpointRequired: policy.checkpoint_required,
  }
}

function batchCommandIdentityBinding(): CommandIdentityBinding | undefined {
  const projectRoot = envDefault("SMELL_PROJECT_ROOT")
  const smell = envDefault("SMELL_SMELL")
  const location = envDefault("SMELL_LOCATION")
  const verificationMode = envDefault("SMELL_VERIFICATION_MODE")
  if (!projectRoot || !smell || !location || !verificationMode) return undefined
  return {
    project_root: projectRoot,
    project_override_root: envDefault("SMELL_CANONICAL_PROJECT_ROOT") || "",
    language: envDefault("SMELL_LANGUAGE") || "",
    smell,
    location,
    target_context_json: envDefault("SMELL_TARGET_CONTEXT_JSON") || "",
    verification_mode: verificationMode,
    sample_test_location: envDefault("SMELL_SAMPLE_TEST_LOCATION") || "",
    sample_test_command: envDefault("SMELL_SAMPLE_TEST_COMMAND") || "",
  }
}

function assertRestoredCommandIdentity(policy: CommandPolicy): void {
  const binding = batchCommandIdentityBinding()
  if (!binding) {
    throw new Error(
      "COMMAND_POLICY_STATE_CONTROLLER_IDENTITY_MISSING: restored state requires "
      + "SMELL_PROJECT_ROOT, SMELL_SMELL, SMELL_LOCATION, and SMELL_VERIFICATION_MODE",
    )
  }
  const mismatches = (Object.keys(binding) as Array<keyof CommandIdentityBinding>)
    .filter((key) => policy.identity[key] !== binding[key])
  if (mismatches.length) {
    throw new Error(
      `COMMAND_POLICY_STATE_IDENTITY_MISMATCH: restored state differs from controller fields: ${mismatches.join(", ")}`,
    )
  }
}

function isJavaCheckpointIdentity(input: {
  checkpointRequired?: unknown
  language?: unknown
  smell?: unknown
  location?: unknown
}): boolean {
  if (input.checkpointRequired !== true || !String(input.smell || "").trim()) return false
  const language = String(input.language || "").trim().toLowerCase()
  const location = String(input.location || "").trim().toLowerCase()
  return language === "java" || /\.java(?::|\b)/.test(location)
}

function isJavaSourceIdentity(input: { language?: unknown; location?: unknown }): boolean {
  const language = String(input.language || "").trim().toLowerCase()
  const location = String(input.location || "").trim().toLowerCase()
  return language === "java" || /\.java(?::|\b)/.test(location)
}

const MAX_STDOUT_STDERR_LEN = 4000

// --- session.idle command-policy continuation -------------------------------
//
// smell_verify attaches the authoritative command-owned loop decision and this
// runtime resumes interactive sessions after session.idle when that decision is
// `continue`. Batch runs set SMELL_BATCH_RUN=1 and synchronously resume the same
// session from run_smell_dataset.py, so the plugin must not dispatch a competing
// prompt. Command policy remains the single source of truth for the retry budget.

const SMELL_IDLE_CONTINUE_PREFIX = "[smell-auto-continue"
const IDLE_CONTINUE_STATE_TTL_MS = 30 * 60 * 1000
const DIRECT_BUILD_COMMAND_RE =
  /(?:^|[;&|]\s*)(?:[A-Za-z_][A-Za-z0-9_]*=[^\s]+\s+)*(?:\.\/)?(?:mvnw|mvn|gradlew|gradle)\b/

function shouldPluginHandleSessionIdle(
  env: Readonly<Record<string, string | undefined>> = process.env,
): boolean {
  return String(env.SMELL_BATCH_RUN || "").trim() !== "1"
}

type FailureClassification = {
  category: string
  verifyStatus: string
  highlights: string[]
  artifactPaths: string[]
  nextAction: string
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
  nextAction: string
  allowTestChanges: boolean
  updatedAt: number
}

// The bridge contract emits failure_pack.artifact_paths as name -> path.
function artifactPathsFrom(value: unknown): string[] {
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
    category: "",
    verifyStatus: "",
    highlights: [],
    artifactPaths: [],
    nextAction: "",
  }
  if (!failurePack || typeof failurePack !== "object" || Array.isArray(failurePack)) {
    return empty
  }
  const pack = failurePack as Record<string, unknown>
  const category = typeof pack.failure_category === "string" ? pack.failure_category.trim() : ""
  const verifyStatus = typeof pack.verify_status === "string" ? pack.verify_status.trim() : ""
  const highlights = asStringArray(pack.highlights)
  const artifactPaths = artifactPathsFrom(pack.artifact_paths)
  const nextAction = typeof pack.next_action === "string" ? pack.next_action.trim() : ""
  return {
    category,
    verifyStatus,
    highlights,
    artifactPaths,
    nextAction,
  }
}

function makeTaskKey(projectRoot: string, smell: string, location: string): string {
  return [String(projectRoot || ""), String(smell || ""), String(location || "")].join("|")
}

function buildContinuationMessage(state: ContinuationState): string {
  return [
    `${SMELL_IDLE_CONTINUE_PREFIX} ${state.continuation}/${state.maxContinuations}]`,
    "Resume the existing task in this session.",
    "Read the latest smell_verify tool result and follow its loop.instruction.",
    "After one narrow corrective edit, call smell_verify again.",
  ].join("\n")
}

function buildVerifyRequiredMessage(state: ContinuationState): string {
  return [
    `${SMELL_IDLE_CONTINUE_PREFIX} verify-required/${state.awaitingVerifyReason}/${state.generation}]`,
    "Resume the existing task in this session and call smell_verify now.",
    "The controller policy is unchanged; use the current source state.",
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

function normalizeBridgeContractPayload(json: unknown, exitCode: number): Record<string, unknown> {
  const jsonPayload =
    json && typeof json === "object" && !Array.isArray(json)
      ? (json as Record<string, unknown>)
      : { value: toJsonSafe(json) }
  const status = typeof jsonPayload.status === "string" ? jsonPayload.status.trim() : ""
  const hasStatus = status.length > 0
  const hasSuccess = typeof jsonPayload.success === "boolean"
  const validVerifySuccess =
    hasStatus &&
    hasSuccess &&
    jsonPayload.success === true &&
    status === "PASS"
  const validStructuredFailure =
    hasStatus &&
    hasSuccess &&
    jsonPayload.success === false &&
    status !== "PASS"

  // normalizeToolResult currently wraps only the bridge `verify` command, whose
  // sole successful top-level status is PASS. Baseline success statuses such as
  // BASELINE_CAPTURED are consumed by their dedicated command hook instead.
  if (exitCode === 0 && validVerifySuccess) {
    return {
      ...jsonPayload,
      success: true,
      status,
    }
  }

  // A structured bridge failure remains useful even when the bridge returns a
  // non-zero process status. Preserve its domain-specific status while forcing
  // success=false. Successful, incomplete, or contradictory payloads fail
  // closed to a transport/contract status instead.
  if (validStructuredFailure) {
    return {
      ...jsonPayload,
      success: false,
      status,
    }
  }

  const fallbackStatus = exitCode === 0 ? "BRIDGE_CONTRACT_INVALID" : "BRIDGE_FAILED"
  const fallbackError =
    exitCode === 0
      ? "Python bridge JSON payload must contain a consistent verify result: success=true only with status=PASS; failures require success=false and a non-PASS status."
      : "Python bridge exited non-zero without a valid structured failure result."
  const existingError =
    typeof jsonPayload.error === "string" && jsonPayload.error.trim()
      ? jsonPayload.error
      : fallbackError
  return {
    ...jsonPayload,
    success: false,
    status: fallbackStatus,
    error: existingError,
  }
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
  const jsonPayload = normalizeBridgeContractPayload(result.json, fields.exitCode)
  return safeJsonStringify({
    ...jsonPayload,
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

function recordValue(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null
}

function arrayValue(value: unknown): unknown[] {
  return Array.isArray(value) ? value : []
}

function compactTarget(payload: Record<string, unknown> | null): Record<string, unknown> | null {
  if (!payload) return null
  const source = recordValue(payload.resolvedSubject) || recordValue(payload.resolvedContext)
  if (!source) return null
  const selected: Record<string, unknown> = {}
  for (const key of [
    "stableTargetId",
    "stableOwnerId",
    "filePath",
    "kind",
    "symbol",
    "displayName",
    "ownerKind",
    "ownerType",
    "ownerSymbol",
    "selectionKind",
    "selectionTextPreview",
  ]) {
    const value = source[key]
    if (value !== undefined && value !== null && value !== "") selected[key] = value
  }
  return selected
}

function compactInputs(payload: Record<string, unknown> | null): unknown[] {
  return arrayValue(payload?.inputs).map((input) => {
    const source = recordValue(input)
    if (!source) return input
    return {
      name: source.name,
      type: source.type,
      required: source.required,
      multiple: source.multiple,
      choices: arrayValue(source.choices).map((choice) => {
        const candidate = recordValue(choice)
        return candidate ? { value: candidate.value, label: candidate.label } : choice
      }),
    }
  })
}

function compactDecisions(payload: Record<string, unknown> | null): unknown[] {
  return arrayValue(payload?.decisions).map((decision) => {
    const source = recordValue(decision)
    if (!source) return decision
    return {
      id: source.id,
      kind: source.kind,
      summary: source.summary,
      recommended: source.recommended,
      choices: arrayValue(source.choices).map((choice) => {
        const candidate = recordValue(choice)
        return candidate
          ? {
              value: candidate.value,
              label: candidate.label,
              inputs: compactInputs({ inputs: candidate.inputs }),
            }
          : choice
      }),
    }
  })
}

function selectionCandidates(payload: Record<string, unknown> | null, operation: string): unknown[] {
  const group = arrayValue(payload?.operationCandidates)
    .map(recordValue)
    .find((candidate) => candidate?.operation === operation)
  return arrayValue(group?.candidates)
}

function numberField(source: Record<string, unknown>, ...keys: string[]): number | undefined {
  for (const key of keys) {
    const value = source[key]
    if (typeof value === "number" && Number.isFinite(value)) return value
  }
  return undefined
}

function selectionCandidatesWithNextRequest(
  payload: Record<string, unknown> | null,
  operation: string,
  request?: IdeaPreviewRequest,
): unknown[] {
  const candidates = selectionCandidates(payload, operation)
  if (!request) return candidates
  const target = compactTarget(payload)
  const targetFile = typeof target?.filePath === "string" ? target.filePath : request.file
  return candidates.map((candidate) => {
    const source = recordValue(candidate)
    if (!source || !targetFile) return candidate
    const nestedSelection = recordValue(source.selection)
    const startLine = numberField(source, "selection-start-line", "selectionStartLine", "startLine")
      ?? (nestedSelection ? numberField(nestedSelection, "startLine") : undefined)
    const startColumn = numberField(source, "selection-start-column", "selectionStartColumn", "startColumn")
      ?? (nestedSelection ? numberField(nestedSelection, "startColumn") : undefined)
    const endLine = numberField(source, "selection-end-line", "selectionEndLine", "endLine")
      ?? (nestedSelection ? numberField(nestedSelection, "endLine") : undefined)
    const endColumn = numberField(source, "selection-end-column", "selectionEndColumn", "endColumn")
      ?? (nestedSelection ? numberField(nestedSelection, "endColumn") : undefined)
    if (
      startLine === undefined
      || startColumn === undefined
      || endLine === undefined
      || endColumn === undefined
    ) return candidate
    const selection = { startLine, startColumn, endLine, endColumn }
    const args: Record<string, unknown> = {
      ideaProjectRoot: request.projectRoot,
      operation,
      file: targetFile,
      line: startLine,
      column: startColumn,
      selection,
    }
    if (request.arguments) args.arguments = request.arguments
    if (request.detail) args.detail = request.detail
    return {
      ...source,
      selection,
      nextRequest: {
        tool: "idea_refactor_preview",
        args,
      },
    }
  })
}

function diagnosticCodes(payload: Record<string, unknown> | null): string[] {
  return arrayValue(payload?.diagnostics)
    .map(recordValue)
    .map((diagnostic) => typeof diagnostic?.code === "string" ? diagnostic.code : "")
    .filter(Boolean)
}

function nextProtocolAction(payload: Record<string, unknown> | null, fallback: string): string {
  const example = recordValue(payload?.nextCliCommandExample)
  const action = typeof example?.action === "string" ? example.action : ""
  if (action === "prepare") return "preview"
  if (action === "apply") return "apply"
  return fallback
}

function nextRequestFromCliExample(
  payload: Record<string, unknown> | null,
  operation: string,
  proposalId: string,
  projectRoot = "",
): Record<string, unknown> | null {
  if (!proposalId) return null
  const example = recordValue(payload?.nextCliCommandExample)
  const action = typeof example?.action === "string" ? example.action : ""
  if (action !== "prepare" && action !== "apply") return null
  const args: Record<string, unknown> = {}
  if (projectRoot) args.ideaProjectRoot = projectRoot
  if (action === "prepare") args.operation = operation
  args.proposalId = proposalId
  const argumentsJson = recordValue(example?.argumentsJson)
  const decisionsJson = recordValue(example?.decisionsJson)
  if (argumentsJson) args.arguments = argumentsJson
  if (decisionsJson) args.decisions = decisionsJson
  return {
    tool: action === "prepare" ? "idea_refactor_preview" : "idea_refactor_apply",
    args,
  }
}

function previewRetryNextRequest(
  request: IdeaPreviewRequest | undefined,
  proposalId: string,
): Record<string, unknown> | null {
  if (!request) return null
  const args: Record<string, unknown> = {
    ideaProjectRoot: request.projectRoot,
    operation: request.operation,
  }
  if (proposalId) {
    args.proposalId = proposalId
  } else if (request.target) {
    args.target = request.target
  } else if (request.file && request.line !== undefined && request.column !== undefined) {
    args.file = request.file
    args.line = request.line
    args.column = request.column
    if (request.selection) args.selection = request.selection
  } else {
    return null
  }
  if (request.arguments) args.arguments = request.arguments
  if (request.decisions) args.decisions = request.decisions
  if (request.detail) args.detail = request.detail
  return { tool: "idea_refactor_preview", args }
}

function ideaProtocolError(
  title: string,
  code: string,
  summary: string,
  nextRequest: Record<string, unknown> | null = null,
): { title: string; output: string; metadata: Record<string, unknown> } {
  const output: Record<string, unknown> = {
    success: false,
    status: "needs_input",
    nextAction: "preview",
    diagnostics: [{ code, summary }],
    validRequestShapes: [
      "initial semantic target: operation + target",
      "initial position target: operation + file + line + column (+ optional selection)",
      "continuation: operation + proposalId (+ arguments/decisions), without target or position",
    ],
  }
  if (nextRequest) output.nextRequest = nextRequest
  return {
    title,
    output: safeJsonStringify(output),
    metadata: { exitCode: 1, stderr: "" },
  }
}

function recoverInvalidPreviewTarget(request: IdeaPreviewRequest): Record<string, unknown> | null {
  const args: Record<string, unknown> = {
    ideaProjectRoot: request.projectRoot,
    operation: request.operation,
  }
  if (request.proposalId) {
    args.proposalId = request.proposalId
  } else if (request.target && (!request.selection || !request.file || request.line === undefined || request.column === undefined)) {
    args.target = request.target
  } else if (request.file && request.line !== undefined && request.column !== undefined) {
    args.file = request.file
    args.line = request.line
    args.column = request.column
    if (request.selection) args.selection = request.selection
  } else {
    return null
  }
  if (request.arguments) args.arguments = request.arguments
  if (request.decisions) args.decisions = request.decisions
  if (request.detail) args.detail = request.detail
  return { tool: "idea_refactor_preview", args }
}

function renderIdeaPreviewProtocolResult(input: {
  operation: string
  proposalId: string
  locate?: IdeaCliResult
  prepare?: IdeaCliResult
  request?: IdeaPreviewRequest
  statusOverride?: "needs_selection" | "unsupported_target"
  timingsMs: Record<string, number>
  detail: IdeaDetail
  wrapperMetadata?: Record<string, unknown>
}): { title: string; output: string; metadata: Record<string, unknown> } {
  const locatePayload = recordValue(input.locate?.json)
  const preparePayload = recordValue(input.prepare?.json)
  const activePayload = preparePayload || locatePayload
  const activeResult = input.prepare || input.locate
  const payloadStatus = typeof activePayload?.status === "string"
    ? activePayload.status.toLowerCase()
    : ""
  const candidates = selectionCandidatesWithNextRequest(locatePayload, input.operation, input.request)
  let status = input.statusOverride || "failed"
  if (!input.statusOverride) {
    status =
      activeResult?.exitCode === 3 && payloadStatus === "retryable_failed"
        ? "retryable_failed"
        : input.prepare?.exitCode === 0 && payloadStatus === "ok"
          ? "ready"
          : input.prepare?.exitCode === 0 && payloadStatus === "needs_decision"
            ? "needs_decision"
            : input.prepare?.exitCode === 0 && payloadStatus === "needs_more_info"
              ? "needs_input"
              : "failed"
  }
  const nextAction =
    status === "ready"
      ? "apply"
      : status === "needs_selection" || status === "needs_input" || status === "retryable_failed"
        ? "preview"
        : status === "needs_decision"
          ? nextProtocolAction(activePayload, "preview")
          : "none"
  const lastResult = input.prepare || input.locate
  const projectRoot = input.request?.projectRoot
    || (typeof input.wrapperMetadata?.project_root === "string" ? input.wrapperMetadata.project_root : "")
  const nextRequest = status === "retryable_failed"
    ? previewRetryNextRequest(input.request, input.proposalId)
    : status === "ready" || status === "needs_input" || status === "needs_decision"
      ? nextRequestFromCliExample(activePayload, input.operation, input.proposalId, projectRoot)
      : null
  const output: Record<string, unknown> = {
    success: status === "ready",
    protocol: "idea-proposal-v1",
    status,
    proposalId: input.proposalId,
    targetId: compactTarget(activePayload)?.stableTargetId || compactTarget(locatePayload)?.stableTargetId || "",
    operation: input.operation,
    nextAction,
    target: compactTarget(activePayload) || compactTarget(locatePayload),
    inputs: compactInputs(preparePayload),
    decisions: compactDecisions(activePayload),
    selectionCandidates: candidates,
    diagnostics: arrayValue(activePayload?.diagnostics),
    timingsMs: input.timingsMs,
  }
  if (nextRequest) output.nextRequest = nextRequest
  if (input.detail === "full") {
    output.raw = {
      locate: locatePayload,
      prepare: preparePayload,
    }
  }
  return {
    title: "IDEA refactor preview",
    output: safeJsonStringify(output),
    metadata: lastResult
      ? normalizeMetadata(lastResult, {
          protocol: "idea-proposal-v1",
          phase: "preview",
          ...input.wrapperMetadata,
        })
      : {
          exitCode: status === "ready" ? 0 : 1,
          stderr: "",
          protocol: "idea-proposal-v1",
          phase: "preview",
          ...input.wrapperMetadata,
        },
  }
}

function renderIdeaApplyProtocolResult(
  proposalId: string,
  result: IdeaCliResult,
  detail: IdeaDetail,
  elapsedMs: number,
  wrapperMetadata: Record<string, unknown> = {},
): { title: string; output: string; metadata: Record<string, unknown> } {
  const payload = recordValue(result.json)
  const payloadStatus = typeof payload?.status === "string" ? payload.status.toLowerCase() : ""
  const codes = diagnosticCodes(payload)
  const requestTimedOut = codes.includes("SERVICE_REQUEST_TIMEOUT")
  let status =
    requestTimedOut
      ? "outcome_unknown"
      : result.exitCode === 0 && payloadStatus === "ok" && payload?.applied === true
      ? "applied"
      : result.exitCode === 0 && payloadStatus === "needs_decision"
        ? "needs_decision"
        : result.exitCode === 0 && payloadStatus === "needs_more_info"
          ? "needs_input"
          : result.exitCode === 3 && payloadStatus === "retryable_failed"
            ? "retryable_failed"
            : "failed"
  if (codes.includes("STALE_DRAFT")) status = "stale"
  const nextAction =
    status === "applied"
      ? "verify"
      : status === "outcome_unknown"
        ? "verify"
      : status === "needs_decision"
        ? nextProtocolAction(payload, "apply")
        : status === "needs_input" || status === "retryable_failed"
          ? "apply"
          : status === "stale"
            ? "preview"
            : "none"
  const applyResult = recordValue(payload?.result)
  const projectRoot = typeof wrapperMetadata.project_root === "string" ? wrapperMetadata.project_root : ""
  const nextRequest = status === "needs_decision" || status === "needs_input" || status === "retryable_failed"
    ? nextRequestFromCliExample(
        payload,
        typeof payload?.operation === "string" ? payload.operation : "",
        proposalId,
        projectRoot,
      )
    : null
  const output: Record<string, unknown> = {
    success: status === "applied",
    protocol: "idea-proposal-v1",
    status,
    proposalId,
    operation: payload?.operation || "",
    nextAction,
    appliedRevision: payload?.appliedRevision || null,
    currentRevision: payload?.currentRevision || null,
    changedFiles: applyResult?.changedFiles ?? null,
    changedFilePaths: arrayValue(applyResult?.changedFilePaths),
    decisions: compactDecisions(payload),
    diagnostics: arrayValue(payload?.diagnostics),
    postApplyProblems: payload?.postApplyProblems || null,
    rollbackAvailable: status === "applied",
    timingsMs: { apply: elapsedMs, total: elapsedMs },
  }
  if (nextRequest) output.nextRequest = nextRequest
  if (detail === "full") output.raw = payload
  return {
    title: "IDEA refactor apply",
    output: safeJsonStringify(output),
    metadata: normalizeMetadata(result, {
      protocol: "idea-proposal-v1",
      phase: "apply",
      ...wrapperMetadata,
    }),
  }
}

async function runIdeaPreviewProtocol(input: {
  worktree: string
  cli: string
  request: IdeaPreviewRequest
  wrapperMetadata?: Record<string, unknown>
  runner?: IdeaCliRunner
}): Promise<{ title: string; output: string; metadata: Record<string, unknown> }> {
  const runCli = input.runner || runIdeaCli
  const request = input.request
  const detail = request.detail || "compact"
  const hasProposal = Boolean(request.proposalId)
  const hasTarget = Boolean(request.target)
  const hasPosition = Boolean(request.file || request.line !== undefined || request.column !== undefined || request.selection)
  if (hasProposal && (hasTarget || hasPosition)) {
    return ideaProtocolError(
      "IDEA refactor preview",
      "INVALID_PREVIEW_TARGET",
      "proposalId continuation must not include target, file, caret, or selection.",
      recoverInvalidPreviewTarget(request),
    )
  }
  if (!hasProposal && hasTarget === hasPosition) {
    return ideaProtocolError(
      "IDEA refactor preview",
      "INVALID_PREVIEW_TARGET",
      "Initial preview requires exactly one target form: semantic target or file with line and column.",
      recoverInvalidPreviewTarget(request),
    )
  }
  if (!hasProposal && hasPosition && (!request.file || request.line === undefined || request.column === undefined)) {
    return ideaProtocolError(
      "IDEA refactor preview",
      "INVALID_PREVIEW_TARGET",
      "Position preview requires file, line, and column.",
      recoverInvalidPreviewTarget(request),
    )
  }

  const startedAt = Date.now()
  let locate: IdeaCliResult | undefined
  let proposalId = request.proposalId || ""
  let locateMs = 0
  if (!proposalId) {
    const locateArgs = ["locate", "--project-root", request.projectRoot]
    if (request.target) {
      locateArgs.push(
        "--body-json",
        JSON.stringify({
          projectRoot: request.projectRoot,
          target: request.target,
          suggestSelectionsFor: [request.operation],
        }),
      )
    } else {
      const resolvedFile = resolveIdeaFile(request.file || "", request.projectRoot)
      if (!resolvedFile.ok) return resolvedFile.result
      locateArgs.push(
        "--file",
        resolvedFile.file,
        "--line",
        String(request.line),
        "--column",
        String(request.column),
        "--suggest-selections-for",
        request.operation,
      )
      addNumber(locateArgs, "--selection-start-line", request.selection?.startLine)
      addNumber(locateArgs, "--selection-start-column", request.selection?.startColumn)
      addNumber(locateArgs, "--selection-end-line", request.selection?.endLine)
      addNumber(locateArgs, "--selection-end-column", request.selection?.endColumn)
    }
    const locateStartedAt = Date.now()
    locate = await runCli(input.worktree, input.cli, locateArgs)
    locateMs = Date.now() - locateStartedAt
    const locatePayload = recordValue(locate.json)
    proposalId = typeof locatePayload?.draftId === "string" ? locatePayload.draftId : ""
    if (locate.exitCode !== 0 || locatePayload?.status !== "ok" || !proposalId) {
      return renderIdeaPreviewProtocolResult({
        operation: request.operation,
        proposalId,
        locate,
        request,
        timingsMs: { locate: locateMs, prepare: 0, total: Date.now() - startedAt },
        detail,
        wrapperMetadata: input.wrapperMetadata,
      })
    }
    if (!operationsFrom(locatePayload).includes(request.operation)) {
      const candidates = selectionCandidates(locatePayload, request.operation)
      return renderIdeaPreviewProtocolResult({
        operation: request.operation,
        proposalId,
        locate,
        request,
        statusOverride: candidates.length ? "needs_selection" : "unsupported_target",
        timingsMs: { locate: locateMs, prepare: 0, total: Date.now() - startedAt },
        detail,
        wrapperMetadata: input.wrapperMetadata,
      })
    }
  }

  const prepareArgs = [
    "prepare",
    "--project-root",
    request.projectRoot,
    "--draft-id",
    proposalId,
    "--operation",
    request.operation,
  ]
  addJson(prepareArgs, "--arguments-json", request.arguments)
  addJson(prepareArgs, "--decisions-json", request.decisions)
  const prepareStartedAt = Date.now()
  const prepare = await runCli(input.worktree, input.cli, prepareArgs)
  const prepareMs = Date.now() - prepareStartedAt
  return renderIdeaPreviewProtocolResult({
    operation: request.operation,
    proposalId,
    locate,
    prepare,
    request,
    timingsMs: { locate: locateMs, prepare: prepareMs, total: Date.now() - startedAt },
    detail,
    wrapperMetadata: input.wrapperMetadata,
  })
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
  const normalizedStatus = payloadStatus.toLowerCase()
  const transportSuccess =
    fields.exitCode === 0 &&
    normalizedStatus !== "failed" &&
    normalizedStatus !== "error" &&
    Boolean(rawPayload)
  const actionRequired =
    normalizedStatus === "needs_decision"
      ? "decision"
      : normalizedStatus === "needs_more_info"
        ? "input"
        : ""
  const complete = transportSuccess && actionRequired === ""
  const nextCommandExample =
    rawPayload &&
    typeof rawPayload.nextCliCommandExample === "object" &&
    rawPayload.nextCliCommandExample !== null &&
    !Array.isArray(rawPayload.nextCliCommandExample)
      ? (rawPayload.nextCliCommandExample as Record<string, unknown>)
      : null
  const nextAction =
    nextCommandExample && typeof nextCommandExample.action === "string"
      ? nextCommandExample.action
      : ""
  const success = complete
  const status =
    payloadStatus ||
    (transportSuccess ? "IDEA_OK" : fields.exitCode === 0 ? "IDEA_EMPTY_PAYLOAD" : "IDEA_FAILED")
  return {
    title: typeof title === "string" && title ? title : "IDEA refactor result",
    output: safeJsonStringify({
      success,
      transport_success: transportSuccess,
      complete,
      action_required: actionRequired,
      next_action: nextAction,
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
// loop decision already attached by applyCommandLoopDecision and never owns a
// second budget. Batch transport is runner-owned, so this runtime records and
// dispatches idle prompts only when shouldPluginHandleSessionIdle allows it.
function createIdleContinueRuntime(options: {
  client?: { session: { promptAsync: (opts: unknown) => Promise<unknown> } }
  log?: (msg: string, details?: unknown) => void
  env?: Readonly<Record<string, string | undefined>>
}) {
  const log = options.log || (() => {})
  const sessionIdleEnabled = shouldPluginHandleSessionIdle(options.env || process.env)
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
    allowTestChanges: boolean
  }) {
    if (!sessionIdleEnabled) return
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
      nextAction: "",
      allowTestChanges: input.allowTestChanges,
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
    allowTestChanges: boolean
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
      enabled: sessionIdleEnabled,
      continuation,
      maxContinuations,
      generation: existing ? existing.generation : 0,
      status,
      category: category || (existing ? existing.failureCategory : ""),
      dispatched: existing ? existing.dispatchedGeneration === existing.generation : false,
    }

    if (!sessionIdleEnabled) {
      if (input.sessionID) states.delete(input.sessionID)
      return { ...base, dispatched: false }
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
          nextAction: classification.nextAction,
          allowTestChanges: input.allowTestChanges,
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
      nextState.nextAction = classification.nextAction
      nextState.allowTestChanges = input.allowTestChanges
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
    if (!sessionIdleEnabled) return false
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

function parseCommandPolicyPayload(value: unknown): CommandPolicy {
  const payload = recordValue(value)
  const loop = recordValue(payload?.loop)
  const identity = recordValue(payload?.identity)
  const verificationMode = payload?.verification_mode
  const allowedModes: VerificationMode[] = ["local", "auto", "sample_optimized", "project_full"]
  const allowedFailureGroups = new Set(["smell", "compile", "test"])
  const requiredIdentityStrings = ["project_root", "smell", "location"] as const
  const optionalIdentityStrings = [
    "project_override_root",
    "language",
    "target_context_json",
    "sample_test_location",
    "sample_test_command",
  ] as const
  if (!payload || typeof payload.task !== "string" || !payload.task.trim()) {
    throw new Error("INVALID_LOOP_POLICY: resolver returned no task text")
  }
  if (typeof verificationMode !== "string" || !allowedModes.includes(verificationMode as VerificationMode)) {
    throw new Error("INVALID_LOOP_POLICY: resolver returned an unsupported verification mode")
  }
  if (typeof payload.allow_test_changes !== "boolean") {
    throw new Error("INVALID_LOOP_POLICY: resolver returned no test-change policy")
  }
  if (payload.allow_test_changes && verificationMode !== "project_full") {
    throw new Error("TEST_CHANGE_REQUIRES_PROJECT_FULL: restored policy is inconsistent")
  }
  if (!identity) {
    throw new Error("INVALID_COMMAND_TASK_IDENTITY: resolver returned no structured identity")
  }
  for (const key of requiredIdentityStrings) {
    if (typeof identity[key] !== "string" || !identity[key].trim()) {
      throw new Error(`INVALID_COMMAND_TASK_IDENTITY: resolver returned no ${key}`)
    }
  }
  for (const key of optionalIdentityStrings) {
    if (typeof identity[key] !== "string") {
      throw new Error(`INVALID_COMMAND_TASK_IDENTITY: resolver returned invalid ${key}`)
    }
  }
  if (identity.verification_mode !== verificationMode) {
    throw new Error("COMMAND_TASK_VERIFICATION_MODE_MISMATCH: identity and policy differ")
  }
  if (typeof payload.checkpoint_required !== "boolean") {
    throw new Error("INVALID_COMMAND_TASK_IDENTITY: resolver returned no checkpoint requirement")
  }
  if (!loop || !["off", "verify-failure"].includes(String(loop.mode || ""))) {
    throw new Error("INVALID_LOOP_POLICY: resolver returned an invalid loop mode")
  }
  if (!Number.isInteger(loop.max_continuations) || Number(loop.max_continuations) < 0 || Number(loop.max_continuations) > 5) {
    throw new Error("INVALID_LOOP_POLICY: resolver returned an invalid continuation limit")
  }
  if (!Number.isInteger(loop.no_progress_limit) || Number(loop.no_progress_limit) < 1 || Number(loop.no_progress_limit) > 5) {
    throw new Error("INVALID_LOOP_POLICY: resolver returned an invalid no-progress limit")
  }
  if (
    !Array.isArray(loop.allowed_failure_groups)
    || !loop.allowed_failure_groups.every((item) => typeof item === "string" && allowedFailureGroups.has(item))
    || (loop.mode !== "off" && Number(loop.max_continuations) > 0 && loop.allowed_failure_groups.length === 0)
  ) {
    throw new Error("INVALID_LOOP_POLICY: resolver returned invalid failure groups")
  }
  if (typeof loop.instruction !== "string" || !loop.instruction.trim()) {
    throw new Error("INVALID_LOOP_POLICY: resolver returned no continuation instruction")
  }
  if (
    !Number.isFinite(loop.sample_deadline_seconds)
    || Number(loop.sample_deadline_seconds) < 60
    || Number(loop.sample_deadline_seconds) > 7200
  ) {
    throw new Error("INVALID_LOOP_POLICY: resolver returned an invalid sample deadline")
  }
  return {
    task: payload.task,
    verification_mode: verificationMode as VerificationMode,
    allow_test_changes: payload.allow_test_changes,
    checkpoint_required: payload.checkpoint_required,
    identity: {
      project_root: identity.project_root as string,
      project_override_root: identity.project_override_root as string,
      language: identity.language as string,
      smell: identity.smell as string,
      location: identity.location as string,
      target_context_json: identity.target_context_json as string,
      verification_mode: identity.verification_mode as VerificationMode,
      sample_test_location: identity.sample_test_location as string,
      sample_test_command: identity.sample_test_command as string,
    },
    loop: {
      mode: loop.mode as LoopPolicy["mode"],
      max_continuations: Number(loop.max_continuations),
      no_progress_limit: Number(loop.no_progress_limit),
      allowed_failure_groups: [...loop.allowed_failure_groups] as string[],
      instruction: loop.instruction,
      sample_deadline_seconds: Number(loop.sample_deadline_seconds),
    },
  }
}

function parseCommandPolicyResult(result: BridgeResult): CommandPolicy {
  if (result.exitCode !== 0 || !result.json || typeof result.json !== "object" || Array.isArray(result.json)) {
    const parsed = result.json as { error?: unknown } | null
    const detail = typeof parsed?.error === "string" ? parsed.error : (result.stderr || "command policy could not be resolved")
    throw new Error(detail)
  }
  return parseCommandPolicyPayload(result.json)
}

function checkpointTargetIdentityPrompt(smell: string, payload: Record<string, unknown> | null): string {
  const metrics = recordValue(payload?.metrics)
  if (!metrics) return ""
  const plan = recordValue(payload?.resolution_plan)
  const guardContract = recordValue(payload?.guard_contract) || recordValue(payload?.finding_contract)
  const findingIdentity = recordValue(guardContract?.entity_identity) || recordValue(metrics.entity_identity) || recordValue(metrics.finding_identity)
  const identity = Object.entries(findingIdentity || {})
    .filter(([, value]) => ["string", "number", "boolean"].includes(typeof value) && String(value).trim())
    .slice(0, 10)
    .map(([key, value]) => `${key}=${String(value)}`)
    .join(", ")
  const forbidden = asStringArray(plan?.forbidden).slice(0, 4)
  const lines = [
    "",
    "Target Guard resolution contract (source-derived; dataset evidence is audit-only):",
    `- Smell: ${smell}.`,
    `- Frozen target: ${identity || String(guardContract?.target_id || guardContract?.finding_id || "the unique target at the supplied location")}.`,
    `- Route family: ${String(plan?.route_family || "close-frozen-finding")}.`,
  ]
  if (forbidden.length) {
    lines.push("- Forbidden pseudo-fixes:")
    for (const item of forbidden) lines.push(`  - ${item}`)
  }
  lines.push("- The frozen target Guard and build/test result are the acceptance authority; do not scan or rewrite unrelated sources.")
  lines.push("- Read mutable remaining counts, worklists, and next actions only from the latest smell_verify tool result.")
  return lines.join("\n")
}

function commandControllerSystemContext(
  policy: CommandPolicy,
  targetIdentityContext: string = "",
  refactoringBackend: string = "direct",
): string {
  const allowed = policy.loop.allowed_failure_groups.join(", ") || "none"
  const backend = refactoringBackend === "idea" ? "idea" : "direct"
  const lines = [
    '<smell-controller-context schema="1">',
    "This stable controller context supplements the original user message; it does not replace it.",
    "Controller-owned verification, identity, and loop policy:",
    "- target_identity: frozen from the original user message and enforced by the controller.",
    `- verification_mode: ${policy.verification_mode}`,
    `- allow_test_changes: ${policy.allow_test_changes}`,
    `- refactoring_backend: ${backend}`,
    `- loop_mode: ${policy.loop.mode}`,
    `- max_continuations: ${policy.loop.max_continuations}`,
    `- no_progress_limit: ${policy.loop.no_progress_limit}`,
    `- allowed_failure_groups: ${allowed}`,
    `- sample_deadline_seconds: ${policy.loop.sample_deadline_seconds}`,
    "",
    "Call smell_verify as the acceptance gate. Its loop.decision field is authoritative.",
    "When loop.decision is continue, read loop.instruction from that tool result and call smell_verify again.",
    "When loop.decision is stop, stop and report loop.termination_reason.",
  ]
  lines.push(
    policy.allow_test_changes
      ? "The controller allows test-source migration for this task. Every changed test is audited and the frozen build/test command must still pass."
      : "Test sources are immutable for this task; TEST_SOURCE_MODIFIED is a verification failure.",
  )
  if (policy.checkpoint_required) {
    lines.push(
      "",
      "Target Guard checkpoint contract:",
      "- Baseline capture must uniquely confirm the requested smell at the supplied target; context selects the entity but never supplies a verdict.",
      "- An unchanged baseline can never pass; make a substantive production-Java refactoring.",
      "- A decreased metric is IMPROVED only. PASS requires the frozen target smell to disappear plus structural and build/test preservation.",
    )
  }
  if (backend === "idea") {
    lines.push(
      "",
      "IDEA refactoring backend contract:",
      "- Load only idea-refactor-cli for the smell-specific route.",
      "- Use the controller-enabled IDEA tools, then call smell_verify.",
      "- Do not invoke the underlying idea-refactor CLI through bash or use OpenCode edit/write tools.",
    )
  }
  if (targetIdentityContext) lines.push(targetIdentityContext)
  lines.push("</smell-controller-context>")
  return lines.join("\n")
}

function newCommandLoopState(policy: CommandPolicy, targetIdentityContext: string = ""): CommandLoopState {
  return {
    policy,
    targetIdentityContext,
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
    target_identity_context: state.targetIdentityContext,
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
    const parsed = recordValue(JSON.parse(raw))
    if (!parsed || parsed.schema_version !== COMMAND_LOOP_STATE_VERSION) return undefined
    const policy = parseCommandPolicyPayload(parsed.policy)
    const loop = policy.loop
    const startedAt = Number(parsed.started_at)
    const continuationCount = Number(parsed.continuation_count)
    const noProgressCount = Number(parsed.no_progress_count)
    const targetIdentityContext = parsed.target_identity_context === undefined
      ? ""
      : parsed.target_identity_context
    if (
      !Number.isFinite(startedAt)
      || !Number.isInteger(continuationCount)
      || continuationCount < 0
      || continuationCount > loop.max_continuations
      || !Number.isInteger(noProgressCount)
      || noProgressCount < 0
      || typeof parsed.cap_recovery_used !== "boolean"
      || typeof parsed.last_failure_fingerprint !== "string"
      || typeof targetIdentityContext !== "string"
      || targetIdentityContext.length > 32768
    ) return undefined
    return {
      policy,
      targetIdentityContext,
      startedAt,
      continuationCount,
      capRecoveryUsed: parsed.cap_recovery_used,
      noProgressCount,
      lastFailureFingerprint: parsed.last_failure_fingerprint,
    }
  } catch {
    return undefined
  }
}

function checkpointTargetIdentityContextFromFile(smell: string, file: string | undefined): string {
  if (!file) return ""
  const parsed = recordValue(JSON.parse(readFileSync(file, "utf8")))
  const payload = recordValue(parsed?.payload) || parsed
  return checkpointTargetIdentityPrompt(smell, payload)
}

function writeControllerContextAudit(context: string, file: string | undefined): void {
  if (!file) return
  const contents = `${context}\n`
  try {
    writeFileSync(file, contents, { encoding: "utf8", flag: "wx" })
  } catch (error) {
    const code = (error as NodeJS.ErrnoException)?.code
    if (code !== "EEXIST" || readFileSync(file, "utf8") !== contents) throw error
  }
}

function hasActionableProgressAtCap(payload: Record<string, unknown>, failureGroup: string): boolean {
  const checkpoint = payload.checkpoint && typeof payload.checkpoint === "object" && !Array.isArray(payload.checkpoint)
    ? payload.checkpoint as Record<string, unknown>
    : undefined
  const delta = checkpoint?.delta && typeof checkpoint.delta === "object" && !Array.isArray(checkpoint.delta)
    ? checkpoint.delta as Record<string, unknown>
    : undefined
  if (delta?.metric_progress === true) return true
  return (failureGroup === "compile" || failureGroup === "test")
    && delta?.has_production_diff === true
}

function failureFingerprint(payload: Record<string, unknown>): string {
  const bridgeFingerprint = typeof payload.failure_fingerprint === "string"
    ? payload.failure_fingerprint.trim()
    : ""
  if (bridgeFingerprint) return bridgeFingerprint

  const pack = payload.failure_pack && typeof payload.failure_pack === "object" && !Array.isArray(payload.failure_pack)
    ? payload.failure_pack as Record<string, unknown>
    : null
  const checkpoint = payload.checkpoint && typeof payload.checkpoint === "object" && !Array.isArray(payload.checkpoint)
    ? payload.checkpoint as Record<string, unknown>
    : null
  const delta = checkpoint?.delta && typeof checkpoint.delta === "object" && !Array.isArray(checkpoint.delta)
    ? checkpoint.delta as Record<string, unknown>
    : null
  const highlights = Array.isArray(pack?.highlights)
    ? pack!.highlights
        .filter((item): item is string => typeof item === "string")
        .slice(0, 3)
        .map((item) => truncateText(item, 1000))
    : []
  const objectives = delta?.objectives && typeof delta.objectives === "object" && !Array.isArray(delta.objectives)
    ? Object.fromEntries(
        Object.entries(delta.objectives as Record<string, unknown>)
          .sort(([left], [right]) => left.localeCompare(right))
          .slice(0, 64)
          .map(([key, value]) => [key, toJsonSafe(value)]),
      )
    : null
  const source = {
    status: truncateText(pack?.verify_status || payload.status || "", 256),
    category: truncateText(pack?.failure_category || "", 256),
    group: truncateText(pack?.failure_group || "", 256),
    nextAction: truncateText(pack?.next_action || "", 1000),
    highlights,
    checkpointDelta: delta ? {
      reason: truncateText(delta.reason || "", 1000),
      objectives,
    } : null,
  }
  return createHash("sha256").update(JSON.stringify(source)).digest("hex")
}

function applyCommandLoopDecision(normalized: { output: string; metadata: Record<string, unknown> }, state: CommandLoopState) {
  let payload: Record<string, unknown>
  try {
    payload = JSON.parse(normalized.output) as Record<string, unknown>
  } catch {
    return
  }
  const resolution = typeof payload.resolution === "string" ? payload.resolution : ""
  const improvedOnly = payload.status === "IMPROVED" || resolution === "improved"
  // A bridge PASS is an atomic contract.  Old or internally inconsistent
  // payloads must never stop the controller loop as accepted work.
  const passed = payload.status === "PASS"
    && resolution === "resolved"
    && payload.success === true
    && payload.accepted === true
  // IMPROVED is durable metric progress, not acceptance. Keep the loop
  // running toward resolved; exhausted budgets retain IMPROVED explicitly.
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
  const nextAction = pack && typeof pack === "object" && !Array.isArray(pack)
    && typeof (pack as Record<string, unknown>).next_action === "string"
    ? String((pack as Record<string, unknown>).next_action).trim()
    : ""
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
      terminationReason = improvedOnly ? "IMPROVED_LOOP_DISABLED" : "LOOP_DISABLED"
    } else if (!improvedOnly && !retryable) {
      terminationReason = "NON_REPAIRABLE_FAILURE"
    } else if (elapsedSeconds >= state.policy.loop.sample_deadline_seconds) {
      terminationReason = improvedOnly ? "IMPROVED_SAMPLE_DEADLINE" : "SAMPLE_DEADLINE_REACHED"
    } else if (state.noProgressCount >= state.policy.loop.no_progress_limit) {
      terminationReason = improvedOnly ? "IMPROVED_NO_PROGRESS" : "NO_PROGRESS"
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
        terminationReason = improvedOnly ? "IMPROVED_MAX_CONTINUATIONS" : "MAX_CONTINUATIONS_REACHED"
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
      ? (nextAction
          ? nextAction
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
  const idleRuntime = createIdleContinueRuntime({ client, env: process.env })
  const refactoringBackend = String(process.env.SMELL_REFACTORING_BACKEND || "direct").trim().toLowerCase()
  const ideaToolsEnabled = refactoringBackend === "idea" && process.env.SMELL_ENABLE_IDEA_TOOLS === "1"
  const commandLoopStates = new Map<string, CommandLoopState>()
  const commandBaselineSeals = new Map<string, string>()
  const restoreBatchCommandState = (sessionID: string): CommandLoopState | undefined => {
    if (!sessionID) return undefined
    const existing = commandLoopStates.get(sessionID)
    if (existing) return existing
    const serializedState = process.env[COMMAND_LOOP_STATE_ENV]
    if (!serializedState) return undefined
    const restored = restoreCommandLoopState(serializedState)
    if (!restored) {
      throw new Error(`COMMAND_POLICY_STATE_INVALID: ${COMMAND_LOOP_STATE_ENV} failed schema validation`)
    }
    assertRestoredCommandIdentity(restored.policy)
    if (!restored.targetIdentityContext && restored.policy.checkpoint_required) {
      restored.targetIdentityContext = checkpointTargetIdentityContextFromFile(
        restored.policy.identity.smell,
        envDefault(BASELINE_CONTEXT_FILE_ENV),
      )
    }
    commandLoopStates.set(sessionID, restored)
    return restored
  }
  const commonShape = {
    projectRoot: tool.schema.string().describe("Absolute path to the source project root."),
    language: tool.schema
      .enum(["java"])
      .optional()
      .describe("Optional source language. Omit to infer from the target file extension."),
    smell: tool.schema.string().describe("Smell type, for example feature_envy or long_method."),
    location: tool.schema.string().describe("Location string, for example src/main/java/Foo.java:88."),
    targetContextJson: tool.schema
      .string()
      .optional()
      .describe("Optional JSON selector context (symbol, receiver, group, or parent identity only; scores, thresholds, and expected verdicts are rejected)."),
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
          .enum(["sample_optimized", "project_full"])
          .optional()
          .describe("Verification mode. Defaults to project_full; every PASS runs configured build/test."),
        noSnapshot: tool.schema.boolean().optional().describe("Do not include git status and source diff snapshot."),
      },
      async execute(args, context) {
        const resolved = withBatchDefaults(args)
        const sessionID = context?.sessionID || ""
        let commandState = commandLoopStates.get(sessionID)
        if (!commandState && sessionID) {
          commandState = restoreBatchCommandState(sessionID)
          if (!commandState) {
            throw new Error(
              "COMMAND_POLICY_STATE_MISSING: smell_verify requires command-owned state or "
              + `${COMMAND_LOOP_STATE_ENV} from the controller`,
            )
          }
        }
        const controllerIdentity = commandState
          ? controllerIdentityFromPolicy(commandState.policy)
          : undefined
        if (isJavaSourceIdentity(resolved) && !controllerIdentity) {
          throw new Error("CHECKPOINT_CONTROLLER_IDENTITY_MISSING: Java checkpoint verification requires command-owned or batch-owned identity")
        }
        if (controllerIdentity) {
          // The command hook captured c000 from this exact controller-owned
          // identity.  Tool arguments are model output and may not retarget a
          // later verification to another finding or verification contract.
          resolved.projectRoot = controllerIdentity.projectRoot
          resolved.projectOverrideRoot = controllerIdentity.projectOverrideRoot
          resolved.language = controllerIdentity.language
          resolved.smell = controllerIdentity.smell
          resolved.location = controllerIdentity.location
          resolved.targetContextJson = controllerIdentity.targetContextJson
          resolved.verificationMode = controllerIdentity.verificationMode
          resolved.sampleTestLocation = controllerIdentity.sampleTestLocation
          resolved.sampleTestCommand = controllerIdentity.sampleTestCommand
          resolved.checkpointRequired = controllerIdentity.checkpointRequired
        }
        const javaCheckpoint = isJavaCheckpointIdentity(resolved)
        if (commandState) {
          resolved.verificationMode = commandState.policy.verification_mode
        }
        const baselineSeal = commandBaselineSeals.get(sessionID) || envDefault("SMELL_BASELINE_SEAL")
        if (javaCheckpoint && !baselineSeal) {
          throw new Error("CHECKPOINT_CONTROLLER_SEAL_MISSING: Java checkpoint verification requires the external baseline seal")
        }
        const bridgeArgs = ["verify", ...commonArgs({ ...resolved, baselineSeal }), "--output-detail", "decision"]
        if (args.noSnapshot) bridgeArgs.push("--no-snapshot")
        const normalized = normalizeToolResult(name, await runBridge(worktree, bridgeArgs))
        if (commandState) {
          applyCommandLoopDecision(normalized, commandState)
          normalized.metadata.command_loop_state = toJsonSafe(commandLoopStateSnapshot(commandState))
        }
        // Every mode consumes the same authoritative loop decision. Interactive
        // surfaces arm plugin-owned idle continuation; batch transport remains
        // exclusively runner-owned and this runtime stays disabled there.
        let autoContinuation: Record<string, unknown> | undefined
        try {
          const cont = idleRuntime.recordFromBridgeOutput({
            sessionID,
            agent: context?.agent || "",
            directory: context?.directory || "",
            taskKey: makeTaskKey(resolved.projectRoot || "", resolved.smell || "", resolved.location || ""),
            output: normalized.output,
            allowTestChanges: commandState?.policy.allow_test_changes === true,
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

  return {
    tool: {
      smell_verify: verifyTool("Smell verification"),

      ...(ideaToolsEnabled ? {
      idea_refactor_preview: tool({
        description:
          "Resolve and prepare one IDEA-native refactoring proposal without changing source. Initial calls require exactly one semantic target or file/caret target. Continue requested inputs or decisions by passing proposalId without a target.",
        args: {
          ...ideaShape,
          operation: tool.schema.string().describe("IDEA refactoring operation, for example extract:method or rename:method."),
          proposalId: tool.schema
            .string()
            .optional()
            .describe("Opaque proposal returned by an earlier preview. When present, omit target, file, caret, and selection."),
          target: tool.schema
            .object({
              fqcn: tool.schema.string().optional(),
              memberName: tool.schema.string().optional(),
              parameterTypes: tool.schema.array(tool.schema.string()).optional(),
              filePath: tool.schema.string().optional(),
              packageName: tool.schema.string().optional(),
              directoryPath: tool.schema.string().optional(),
              moduleName: tool.schema.string().optional(),
            })
            .optional()
            .describe("Stable semantic target. For a zero-argument method, pass parameterTypes: []; omit parameterTypes for a field."),
          file: tool.schema
            .string()
            .optional()
            .describe("Java file path for a caret target, relative to the resolved IDEA project root, dataset root, or absolute."),
          line: tool.schema.number().int().optional().describe("1-based caret line for a position target."),
          column: tool.schema.number().int().optional().describe("1-based caret column for a position target."),
          selection: tool.schema
            .object({
              startLine: tool.schema.number().int().describe("1-based selection start line."),
              startColumn: tool.schema.number().int().describe("1-based selection start column."),
              endLine: tool.schema.number().int().describe("1-based selection end line."),
              endColumn: tool.schema.number().int().describe("1-based selection end column."),
            })
            .optional()
            .describe("Optional explicit selection range."),
          arguments: jsonObjectShape("Known structured operation arguments. Pass them on the first preview."),
          decisions: ideaDecisionsShape(
            "Structured prepare decisions keyed by decision id. Each value must be {choice, arguments?}.",
          ),
          detail: tool.schema
            .enum(["compact", "full"])
            .optional()
            .describe("Response detail. Defaults to compact; full includes the underlying raw IDEA discovery/preparation payloads."),
        },
        async execute(args) {
          const resolved = resolveIdeaInput(args)
          if (!resolved.ok) return resolved.result
          return runIdeaPreviewProtocol({
            worktree,
            cli: resolved.ideaRefactorCli,
            request: {
              projectRoot: resolved.projectRoot,
              operation: args.operation,
              proposalId: args.proposalId,
              target: args.target,
              file: args.file,
              line: args.line,
              column: args.column,
              selection: args.selection,
              arguments: args.arguments,
              decisions: args.decisions,
              detail: args.detail,
            },
            wrapperMetadata: resolved.wrapperMetadata,
          })
        },
      }),

      idea_refactor_apply: tool({
        description:
          "Apply one prepared IDEA proposal by explicit proposalId. Never applies an implicit current draft. Follow nextAction when IDEA requests more input or a structured decision.",
        args: {
          ...ideaShape,
          proposalId: tool.schema.string().describe("Opaque proposalId returned by idea_refactor_preview."),
          arguments: jsonObjectShape("Structured operation arguments. The wrapper serializes this to JSON safely."),
          decisions: ideaDecisionsShape(
            "Structured decisions keyed by decision id. Each value must be {choice, arguments?}.",
          ),
          detail: tool.schema
            .enum(["compact", "full"])
            .optional()
            .describe("Response detail. Defaults to compact; full includes the raw apply payload."),
        },
        async execute(args) {
          const resolved = resolveIdeaInput(args)
          if (!resolved.ok) return resolved.result
          const cliArgs = [
            "apply",
            "--project-root",
            resolved.projectRoot,
            "--draft-id",
            args.proposalId,
          ]
          addJson(cliArgs, "--arguments-json", args.arguments)
          addJson(cliArgs, "--decisions-json", args.decisions)
          const startedAt = Date.now()
          const result = await runIdeaCli(worktree, resolved.ideaRefactorCli, cliArgs)
          return renderIdeaApplyProtocolResult(
            args.proposalId,
            result,
            args.detail || "compact",
            Date.now() - startedAt,
            resolved.wrapperMetadata,
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
          "Revert the most recent successful IDEA apply. This is not for discarding an unapplied proposal.",
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
              warning: "This reverted a previously applied source change, not merely an unapplied proposal.",
            },
          )
        },
      }),
      } : {}),
    },

    "tool.execute.before": async (input, output) => {
      const sessionID = typeof input.sessionID === "string" ? input.sessionID : ""
      if (
        ideaToolsEnabled
        && ["edit", "write", "patch", "apply_patch"].includes(input.tool)
      ) {
        throw new Error(
          "IDEA_BACKEND_DIRECT_EDIT_FORBIDDEN: use idea_refactor_preview and "
          + "idea_refactor_apply; use idea_edit only after a recorded proposal blocker.",
        )
      }
      if (input.tool !== "bash") return
      const command = String(output.args?.command ?? "")
      if (!command) return
      if (ideaToolsEnabled && /\bidea-refactor\b/.test(command)) {
        throw new Error(
          "IDEA_BACKEND_DIRECT_CLI_FORBIDDEN: call idea_refactor_preview or "
          + "idea_refactor_apply instead of invoking the underlying CLI through bash.",
        )
      }
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
        throw new Error("Java source rewrites should use OpenCode edit tools, not shell text rewriting.")
      }
    },

    "command.execute.before": async (input, _output) => {
      if (
        input.command !== "smell-refactor-run" &&
        input.command !== "java-refactor-run"
      ) return
      const result = await runBridge(worktree, ["resolve-command", "--arguments", input.arguments])
      const policy = parseCommandPolicyResult(result)
      const identity = controllerIdentityFromPolicy(policy)
      let targetIdentityPrompt = ""
      if (policy.checkpoint_required) {
        const baselineResult = await runBridge(worktree, [
          "capture-baseline",
          ...commonArgs({
            projectRoot: String(identity.projectRoot),
            projectOverrideRoot: identity.projectOverrideRoot,
            language: identity.language,
            smell: String(identity.smell),
            location: String(identity.location),
            targetContextJson: identity.targetContextJson,
            allowTestChanges: policy.allow_test_changes,
            verificationMode: policy.verification_mode,
            sampleTestLocation: identity.sampleTestLocation,
            sampleTestCommand: identity.sampleTestCommand,
          }),
          "--output-detail",
          "decision",
        ])
        const baselinePayload = baselineResult.json as Record<string, unknown> | null
        if (baselineResult.exitCode !== 0 || !baselinePayload || baselinePayload.success !== true) {
          throw new Error(
            `CHECKPOINT_BASELINE_CAPTURE_FAILED: ${truncateText(baselineResult.stderr || baselineResult.stdout)}`,
          )
        }
        const baselineSeal = String(baselinePayload.baseline_seal || "").trim()
        if (!baselineSeal) {
          throw new Error("CHECKPOINT_BASELINE_CAPTURE_FAILED: controller baseline seal is missing")
        }
        commandBaselineSeals.set(input.sessionID, baselineSeal)
        targetIdentityPrompt = checkpointTargetIdentityPrompt(identity.smell, baselinePayload)
      }
      commandLoopStates.set(input.sessionID, newCommandLoopState(policy, targetIdentityPrompt))
      idleRuntime.clearSession(input.sessionID)
      idleRuntime.armInitialVerification({
        sessionID: input.sessionID,
        agent:
          input.command === "smell-refactor-run"
            ? "smell-refactor-agent"
            : "java-refactor-agent",
        directory: worktree,
        maxContinuations: policy.loop.max_continuations,
        instruction: policy.loop.instruction,
        allowTestChanges: policy.allow_test_changes,
      })
    },

    "experimental.chat.system.transform": async (input, output) => {
      const sessionID = typeof input.sessionID === "string" ? input.sessionID : ""
      if (!sessionID) return
      const state = commandLoopStates.get(sessionID) || restoreBatchCommandState(sessionID)
      if (!state) return
      const context = commandControllerSystemContext(
        state.policy,
        state.targetIdentityContext,
        refactoringBackend,
      )
      if (!output.system.includes(context)) output.system.push(context)
      writeControllerContextAudit(context, envDefault(CONTROLLER_CONTEXT_AUDIT_FILE_ENV))
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
            commandBaselineSeals.delete(sessionID)
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
  normalizeBridgeContractPayload,
  normalizeMetadata,
  normalizeStdioFields,
  safeStringOutput,
  truncateText,
  safeJsonStringify,
  toJsonSafe,
  renderIdeaResult,
  renderIdeaPreviewProtocolResult,
  renderIdeaApplyProtocolResult,
  runIdeaPreviewProtocol,
  selectionCandidates,
  ideaDecisionsShape,
  parseCommandPolicyResult,
  checkpointTargetIdentityPrompt,
  commandControllerSystemContext,
  checkpointTargetIdentityContextFromFile,
  newCommandLoopState,
  commandLoopStateSnapshot,
  restoreCommandLoopState,
  failureFingerprint,
  isJavaCheckpointIdentity,
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
  shouldPluginHandleSessionIdle,
  SMELL_IDLE_CONTINUE_PREFIX,
  IDLE_CONTINUE_STATE_TTL_MS,
}

export default SmellPlugin

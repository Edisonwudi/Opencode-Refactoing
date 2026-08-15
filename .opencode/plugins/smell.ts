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

// snake_case policy key, camelCase controller key, environment variable, CLI flag, CLI order.
const COMMAND_IDENTITY_FIELDS = [
  ["project_root", "projectRoot", "SMELL_PROJECT_ROOT", "--project-root", 0],
  ["project_override_root", "projectOverrideRoot", "SMELL_CANONICAL_PROJECT_ROOT", "--project-override-root", 4],
  ["language", "language", "SMELL_LANGUAGE", "--language", 3],
  ["smell", "smell", "SMELL_SMELL", "--smell", 1],
  ["location", "location", "SMELL_LOCATION", "--location", 2],
  ["target_context_json", "targetContextJson", "SMELL_TARGET_CONTEXT_JSON", "--target-context-json", 5],
  ["verification_mode", "verificationMode", "SMELL_VERIFICATION_MODE", "--verification-mode", 6],
  ["sample_test_location", "sampleTestLocation", "SMELL_SAMPLE_TEST_LOCATION", "--sample-test-location", 7],
  ["sample_test_command", "sampleTestCommand", "SMELL_SAMPLE_TEST_COMMAND", "--sample-test-command", 8],
  ["build_command", "buildCommand", "SMELL_BUILD_COMMAND", "--build-command", 9],
  ["project_test_command", "projectTestCommand", "SMELL_PROJECT_TEST_COMMAND", "--project-test-command", 10],
  ["verification_cwd", "verificationCwd", "SMELL_VERIFICATION_CWD", "--verification-cwd", 11],
  ["verification_command_source", "verificationCommandSource", "SMELL_VERIFICATION_COMMAND_SOURCE", "--verification-command-source", 12],
  ["sample_test_source", "sampleTestSource", "SMELL_SAMPLE_TEST_SOURCE", "--sample-test-source", 13],
] as const

type CommandIdentityKey = (typeof COMMAND_IDENTITY_FIELDS)[number][0]
type CommandTaskIdentity = Record<CommandIdentityKey, string> & { verification_mode: VerificationMode }

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
  buildCommand?: string
  projectTestCommand?: string
  verificationCwd?: string
  verificationCommandSource?: string
  sampleTestSource?: string
  checkpointRequired: boolean
}

type CommandIdentityBinding = Record<CommandIdentityKey, string>
type CommandIdentityEnvironment = Record<CommandIdentityKey, string | undefined>
type BridgeIdentityArgs =
  & Pick<ControllerIdentity, "projectRoot" | "smell" | "location">
  & Omit<ControllerIdentity, "projectRoot" | "smell" | "location" | "verificationMode" | "checkpointRequired">
  & { verificationMode?: string; baselineSeal?: string; allowTestChanges?: boolean }

type CommandLoopState = {
  policy: CommandPolicy
  targetIdentityContext: string
  startedAt: number
  continuationCount: number
  capRecoveryUsed: boolean
  noProgressCount: number
  lastFailureFingerprint: string
}

const COMMAND_LOOP_STATE_VERSION = 4
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

const COMMAND_IDENTITY_CLI_FIELDS = [...COMMAND_IDENTITY_FIELDS]
  .sort((left, right) => left[4] - right[4])

function commandIdentityEnvironment(): CommandIdentityEnvironment {
  return Object.fromEntries(
    COMMAND_IDENTITY_FIELDS.map(([key, , env]) => [key, envDefault(env)]),
  ) as CommandIdentityEnvironment
}

function controllerIdentityFields(
  identity: Readonly<CommandIdentityEnvironment>,
): Partial<Omit<ControllerIdentity, "checkpointRequired">> {
  return Object.fromEntries(
    COMMAND_IDENTITY_FIELDS.map(([key, controllerKey]) => [controllerKey, identity[key] || undefined]),
  ) as Partial<Omit<ControllerIdentity, "checkpointRequired">>
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

function withBatchDefaults(input: Partial<BridgeIdentityArgs> & { [key: string]: unknown }) {
  const envIdentity = commandIdentityEnvironment()
  const envController = controllerIdentityFields(envIdentity)
  const hasBatchIdentity = Boolean(envIdentity.project_root && envIdentity.smell && envIdentity.location)
  return {
    ...input,
    ...envController,
    projectRoot: hasBatchIdentity ? envIdentity.project_root! : input.projectRoot,
    language: hasBatchIdentity ? envIdentity.language : (input.language || envIdentity.language),
    smell: hasBatchIdentity ? envIdentity.smell! : input.smell,
    location: hasBatchIdentity ? envIdentity.location! : input.location,
    targetContextJson: hasBatchIdentity
      ? envIdentity.target_context_json
      : (input.targetContextJson || envIdentity.target_context_json),
    verificationMode: hasBatchIdentity
      ? envIdentity.verification_mode
      : (input.verificationMode || envIdentity.verification_mode),
    checkpointRequired: input.checkpointRequired === true,
  }
}

function commonArgs(input: BridgeIdentityArgs): string[] {
  const args: string[] = []
  for (const [key, controllerKey, , flag] of COMMAND_IDENTITY_CLI_FIELDS) {
    if (key === "target_context_json") addOptional(args, "--baseline-seal", input.baselineSeal)
    if (key === "verification_mode" && input.allowTestChanges) args.push("--allow-test-changes")
    const value = input[controllerKey]
    if (key === "project_root" || key === "smell" || key === "location") {
      args.push(flag, value as string)
    } else {
      addOptional(args, flag, value)
    }
  }
  return args
}

function controllerIdentityFromPolicy(policy: CommandPolicy): ControllerIdentity {
  const identity = policy.identity
  return {
    ...controllerIdentityFields(identity),
    projectRoot: identity.project_root,
    smell: identity.smell,
    location: identity.location,
    verificationMode: identity.verification_mode,
    checkpointRequired: policy.checkpoint_required,
  }
}

function batchCommandIdentityBinding(): CommandIdentityBinding | undefined {
  const identity = commandIdentityEnvironment()
  if (!identity.project_root || !identity.smell || !identity.location || !identity.verification_mode) return undefined
  return Object.fromEntries(
    COMMAND_IDENTITY_FIELDS.map(([key]) => [key, identity[key] || ""]),
  ) as CommandIdentityBinding
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

function usesCheapGuardProgressGate(
  input: { language?: unknown; location?: unknown },
  state: CommandLoopState | undefined,
): boolean {
  const language = String(input.language || "").trim().toLowerCase()
  return state?.policy.checkpoint_required === true
    && (isJavaSourceIdentity(input) || ["python", "c", "cpp"].includes(language))
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
const CAPPED_BUILD_COMMAND_RE =
  /^((?:(?:(?:[^\s;&|]+\/)?env|command)\s+|[A-Za-z_][A-Za-z0-9_]*=(?:"[^"\n]*"|'[^'\n]*'|[^\s;&|]*)\s+)*)(?:[^\s;&|]+\/)?(cmake|gmake|make|ninja)\b(.*)/
const SHELL_LC_COMMAND_RE =
  /^((?:(?:(?:[^\s;&|]+\/)?env|command)\s+|[A-Za-z_][A-Za-z0-9_]*=(?:"[^"\n]*"|'[^'\n]*'|[^\s;&|]*)\s+)*)(?:[^\s;&|]+\/)?(?:bash|sh)\s+-lc\s+(?:"([^"\n]*)"|'([^'\n]*)')/
const CMAKE_BUILD_PARALLEL_LEVEL_RE =
  /(?:^|\s)CMAKE_BUILD_PARALLEL_LEVEL=(?:"([^"\n]*)"|'([^'\n]*)'|([^\s]*))/g
const SMELL_BUILD_JOBS_ASSIGNMENT_RE =
  /(?:^|\s)SMELL_BUILD_JOBS=(?:"([^"\n]*)"|'([^'\n]*)'|([^\s]*))/g
const MAKEFLAGS_ASSIGNMENT_RE =
  /(?:^|\s)MAKEFLAGS=(?:"([^"\n]*)"|'([^'\n]*)'|([^\s]*))/g
const MFLAGS_ASSIGNMENT_RE =
  /(?:^|\s)MFLAGS=(?:"([^"\n]*)"|'([^'\n]*)'|([^\s]*))/g
const CONTROLLER_BUILD_JOBS_EXPRESSION = "${SMELL_BUILD_JOBS:-1}"

type BuildParallelismViolation = {
  tool: string
  requested: number | "unbounded-or-nonliteral"
}

function controllerBuildJobLimit(
  env: Readonly<Record<string, string | undefined>> = process.env,
): number | undefined {
  const raw = String(env.SMELL_BUILD_JOBS || "").trim()
  if (!/^[1-9][0-9]*$/.test(raw)) return undefined
  const value = Number(raw)
  return Number.isSafeInteger(value) ? value : undefined
}

function isCappedCheckpointBuildSession(state: CommandLoopState | undefined): boolean {
  if (state?.policy.checkpoint_required !== true) return false
  const language = String(state.policy.identity.language || "").trim().toLowerCase()
  return ["python", "c", "cpp", "c++"].includes(language)
}

function isProtectedProjectFullCandidateShellPolicy(
  policy: CommandPolicy | undefined,
): boolean {
  return policy?.checkpoint_required === true
    && policy.verification_mode === "project_full"
    && ["python", "c", "cpp", "c++"].includes(
      String(policy.identity.language || "").trim().toLowerCase(),
    )
}

function isProtectedProjectFullCandidateShellSession(
  state: CommandLoopState | undefined,
): boolean {
  return isProtectedProjectFullCandidateShellPolicy(state?.policy)
}

function isControllerBuildJobsExpression(value: string): boolean {
  return value === CONTROLLER_BUILD_JOBS_EXPRESSION
    || value === `"${CONTROLLER_BUILD_JOBS_EXPRESSION}"`
    || value === `'${CONTROLLER_BUILD_JOBS_EXPRESSION}'`
}

function commonHeredocDelimiter(
  line: string,
): { delimiter: string; stripTabs: boolean } | undefined {
  // Recognize only the common literal-delimiter form. Quoted text before the
  // operator is left untouched instead of turning this into a shell parser.
  const match = line.match(
    /^(?:<<|[^'"\n]*[^<]<<)(-)?(?!<)\s*(?:'([A-Za-z_][A-Za-z0-9_]*)'|"([A-Za-z_][A-Za-z0-9_]*)"|([A-Za-z_][A-Za-z0-9_]*))/,
  )
  const delimiter = match?.[2] || match?.[3] || match?.[4]
  return delimiter
    ? { delimiter, stripTabs: match?.[1] === "-" }
    : undefined
}

function withoutCommonHeredocBodies(command: string): string {
  const lines = command.split("\n")
  let active: { delimiter: string; stripTabs: boolean } | undefined
  return lines.map((line) => {
    if (active) {
      const comparable = active.stripTabs ? line.replace(/^\t+/, "") : line
      if (comparable === active.delimiter) active = undefined
      return ""
    }
    active = commonHeredocDelimiter(line)
    return line
  }).join("\n")
}

function executableShellSegments(command: string): string[] {
  const source = withoutCommonHeredocBodies(command)
  const segments: string[] = []
  let current = ""
  let quote: "'" | "\"" | undefined
  const pushCurrent = () => {
    const segment = current.trim()
    if (segment) segments.push(segment)
    current = ""
  }
  for (let index = 0; index < source.length; index += 1) {
    const char = source[index]
    if (char === "\\" && quote !== "'") {
      current += char
      if (index + 1 < source.length) {
        current += source[index + 1]
        index += 1
      }
      continue
    }
    if (quote) {
      current += char
      if (char === quote) quote = undefined
      continue
    }
    if (char === "'" || char === "\"") {
      quote = char
      current += char
      continue
    }
    if (char === "#" && (!current || /\s$/.test(current))) {
      pushCurrent()
      while (index + 1 < source.length && source[index + 1] !== "\n") index += 1
      continue
    }
    if (char === ";" || char === "&" || char === "|" || char === "\n") {
      pushCurrent()
      continue
    }
    current += char
  }
  pushCurrent()
  return segments
}

function requestedParallelismViolation(
  tool: string,
  rawValue: string,
  limit: number,
): BuildParallelismViolation | undefined {
  if (isControllerBuildJobsExpression(rawValue)) return undefined
  const requested = /^[0-9]+$/.test(rawValue) ? Number(rawValue) : 0
  if (Number.isSafeInteger(requested) && requested >= 1 && requested <= limit) {
    return undefined
  }
  return {
    tool,
    requested: requested > 0 && Number.isSafeInteger(requested)
      ? requested
      : "unbounded-or-nonliteral",
  }
}

function cmakeEnvironmentParallelismViolation(
  prefix: string,
  limit: number,
): BuildParallelismViolation | undefined {
  let rawValue: string | undefined
  for (const match of prefix.matchAll(CMAKE_BUILD_PARALLEL_LEVEL_RE)) {
    rawValue = String(match[1] ?? match[2] ?? match[3] ?? "")
  }
  return rawValue === undefined
    ? undefined
    : requestedParallelismViolation("cmake", rawValue, limit)
}

function lastEnvironmentAssignment(
  prefix: string,
  pattern: RegExp,
): string | undefined {
  let rawValue: string | undefined
  for (const match of prefix.matchAll(pattern)) {
    rawValue = String(match[1] ?? match[2] ?? match[3] ?? "")
  }
  return rawValue
}

function makeFlagsParallelismViolation(
  tool: string,
  prefix: string,
  limit: number,
): BuildParallelismViolation | undefined {
  for (const pattern of [MAKEFLAGS_ASSIGNMENT_RE, MFLAGS_ASSIGNMENT_RE]) {
    const rawFlags = lastEnvironmentAssignment(prefix, pattern)
    if (rawFlags === undefined) continue
    const tokens = rawFlags.trim().split(/\s+/).filter(Boolean)
    for (let index = 0; index < tokens.length; index += 1) {
      const token = tokens[index]
      let rawValue: string | undefined
      if (token === "-j" || token === "--jobs") {
        rawValue = tokens[index + 1] || ""
        if (rawValue) index += 1
      } else if (token.startsWith("-j")) {
        rawValue = token.slice(2)
      } else if (token.startsWith("--jobs=")) {
        rawValue = token.slice("--jobs=".length)
      } else if (/^j[0-9]*$/.test(token)) {
        rawValue = token.slice(1)
      } else {
        continue
      }
      const violation = requestedParallelismViolation(tool, rawValue, limit)
      if (violation) return violation
    }
  }
  return undefined
}

function prefixParallelismViolation(
  buildTool: string,
  prefix: string,
  limit: number,
): BuildParallelismViolation | undefined {
  const controllerOverride = lastEnvironmentAssignment(
    prefix,
    SMELL_BUILD_JOBS_ASSIGNMENT_RE,
  )
  if (controllerOverride !== undefined) {
    const violation = requestedParallelismViolation(
      buildTool,
      controllerOverride,
      limit,
    )
    if (violation) return violation
  }
  if (buildTool === "cmake") {
    const violation = cmakeEnvironmentParallelismViolation(prefix, limit)
    if (violation) return violation
  }
  if (["cmake", "gmake", "make"].includes(buildTool)) {
    return makeFlagsParallelismViolation(buildTool, prefix, limit)
  }
  return undefined
}

function explicitBuildParallelismViolation(
  command: string,
  limit: number,
): BuildParallelismViolation | undefined {
  const candidates = [{ command, inheritedPrefix: "" }]
  const seen = new Set<string>()
  for (let candidateIndex = 0; candidateIndex < candidates.length; candidateIndex += 1) {
    const candidate = candidates[candidateIndex]
    const candidateKey = candidate.inheritedPrefix + "\0" + candidate.command
    if (seen.has(candidateKey)) continue
    seen.add(candidateKey)
    for (const segment of executableShellSegments(candidate.command)) {
      const shellMatch = segment.match(SHELL_LC_COMMAND_RE)
      if (shellMatch) {
        const nested = String(shellMatch[2] ?? shellMatch[3] ?? "").trim()
        if (nested) {
          candidates.push({
            command: nested,
            inheritedPrefix: candidate.inheritedPrefix + " " + String(shellMatch[1] || ""),
          })
        }
      }
      const match = segment.match(CAPPED_BUILD_COMMAND_RE)
      if (!match) continue
      const prefix = candidate.inheritedPrefix + " " + String(match[1] || "")
      const buildTool = String(match[2] || "").toLowerCase()
      const args = String(match[3] || "").trim().split(/\s+/).filter(Boolean)
      if (buildTool === "cmake" && args[0] !== "--build") continue
      const prefixViolation = prefixParallelismViolation(buildTool, prefix, limit)
      if (prefixViolation) return prefixViolation
      for (let index = 0; index < args.length; index += 1) {
        const token = args[index]
        let rawValue: string | undefined
        if (token === "-j") {
          const following = args[index + 1]
          if (following && (/^[0-9]+$/.test(following) || isControllerBuildJobsExpression(following))) {
            rawValue = following
            index += 1
          } else {
            rawValue = ""
          }
        } else if (token.startsWith("-j")) {
          rawValue = token.slice(2)
        } else if (buildTool === "cmake" && token === "--parallel") {
          const following = args[index + 1]
          if (following && (/^[0-9]+$/.test(following) || isControllerBuildJobsExpression(following))) {
            rawValue = following
            index += 1
          } else {
            rawValue = ""
          }
        } else if (buildTool === "cmake" && token.startsWith("--parallel=")) {
          rawValue = token.slice("--parallel=".length)
        } else if (buildTool !== "cmake" && token === "--jobs") {
          const following = args[index + 1]
          if (following && (/^[0-9]+$/.test(following) || isControllerBuildJobsExpression(following))) {
            rawValue = following
            index += 1
          } else {
            rawValue = ""
          }
        } else if (buildTool !== "cmake" && token.startsWith("--jobs=")) {
          rawValue = token.slice("--jobs=".length)
        } else {
          continue
        }
        const violation = requestedParallelismViolation(buildTool, rawValue, limit)
        if (violation) return violation
      }
    }
  }
  return undefined
}

function shouldPluginHandleSessionIdle(
  env: Readonly<Record<string, string | undefined>> = process.env,
): boolean {
  return String(env.SMELL_BATCH_RUN || "").trim() !== "1"
}

type ContinuationState = {
  taskKey: string
  generation: number
  dispatchedGeneration: number
  continuation: number
  maxContinuations: number
  pending: boolean
  dispatching: boolean
  awaitingVerify: boolean
  awaitingVerifyReason: "initial" | "continuation"
  verifyReminderGeneration: number
  agent: string
  directory: string
  failureCategory: string
  updatedAt: number
}

type PreparedLoopOutput = {
  payload: Record<string, unknown>
  failureCategory: string
}

function asStringArray(value: unknown): string[] {
  if (!Array.isArray(value)) return []
  return value
    .map((item) => (typeof item === "string" ? item : ""))
    .filter((item) => item.length > 0)
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

type MetricBudgetItem = {
  metric: string
  current: string
  boundaryKey: "passing_max" | "passing_exclusive_max"
  boundary: string
  requiredReduction: string
  unit: string
}

function shortPromptScalar(value: unknown, limit = 96): string | undefined {
  if (typeof value === "number") {
    return Number.isFinite(value) ? String(value) : undefined
  }
  if (typeof value === "boolean") return String(value)
  if (typeof value !== "string") return undefined
  const compact = value.replace(/\s+/g, " ").trim()
  if (!compact) return undefined
  return compact.length <= limit ? compact : compact.slice(0, limit)
}

function checkpointMetricBudget(plan: Record<string, unknown> | null): MetricBudgetItem[] {
  const output: MetricBudgetItem[] = []
  for (const rawItem of arrayValue(plan?.metric_budget)) {
    const item = recordValue(rawItem)
    if (!item) continue
    const metric = shortPromptScalar(item.metric)
    const current = shortPromptScalar(item.current)
    const unit = shortPromptScalar(item.unit, 48)
    const requiredReduction = shortPromptScalar(item.required_reduction)
    const boundaryKey = item.passing_max !== undefined
      ? "passing_max"
      : "passing_exclusive_max"
    const boundary = shortPromptScalar(item[boundaryKey])
    if (!metric || !current || !unit || !requiredReduction || !boundary) continue
    output.push({ metric, current, boundaryKey, boundary, requiredReduction, unit })
    if (output.length >= 12) break
  }
  return output
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
  }) {
    if (!sessionIdleEnabled) return
    if (!input.sessionID) return
    states.set(input.sessionID, {
      taskKey: "",
      generation: 0,
      dispatchedGeneration: -1,
      continuation: 0,
      maxContinuations: input.maxContinuations,
      pending: false,
      dispatching: false,
      awaitingVerify: true,
      awaitingVerifyReason: "initial",
      verifyReminderGeneration: -1,
      agent: input.agent,
      directory: input.directory,
      failureCategory: "",
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
    preparedOutput?: PreparedLoopOutput | null
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
    let preparedOutput = input.preparedOutput
    if (preparedOutput === undefined) {
      try {
        const payload = JSON.parse(input.output) as Record<string, unknown>
        const failurePack = payload.failure_pack && typeof payload.failure_pack === "object" && !Array.isArray(payload.failure_pack)
          ? payload.failure_pack as Record<string, unknown>
          : null
        preparedOutput = {
          payload,
          failureCategory: typeof failurePack?.failure_category === "string"
            ? failurePack.failure_category.trim()
            : "",
        }
      } catch {
        preparedOutput = null
      }
    }
    const parsed = preparedOutput?.payload
    const loop = parsed?.loop && typeof parsed.loop === "object" && !Array.isArray(parsed.loop)
      ? parsed.loop as Record<string, unknown>
      : null

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

    const base = {
      enabled: sessionIdleEnabled,
      continuation,
      maxContinuations,
      generation: existing ? existing.generation : 0,
      status: typeof parsed?.status === "string" ? parsed.status : "",
      category: (typeof parsed?.failure_category === "string" ? parsed.failure_category : "")
        || (existing ? existing.failureCategory : ""),
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

    if (!preparedOutput || decision !== "continue" || continuation <= 0 || continuation > maxContinuations) {
      revokePending()
      return { ...base, dispatched: false }
    }

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
          pending: true,
          dispatching: false,
          awaitingVerify: false,
          awaitingVerifyReason: "continuation",
          verifyReminderGeneration: -1,
          agent: input.agent,
          directory: input.directory,
          failureCategory: preparedOutput.failureCategory,
          updatedAt: Date.now(),
        }
    // When mutating in place, update the fields that changed.
    if (hasInflightDispatch) {
      nextState.taskKey = input.taskKey
      nextState.generation = nextGeneration
      nextState.continuation = continuation
      nextState.maxContinuations = maxContinuations
      nextState.pending = true
      nextState.awaitingVerify = false
      nextState.awaitingVerifyReason = "continuation"
      nextState.verifyReminderGeneration = -1
      nextState.agent = input.agent
      nextState.directory = input.directory
      nextState.failureCategory = preparedOutput.failureCategory
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
  const allowedCommandSources = new Set([
    "command",
    "cli",
    "dataset",
    "project_manifest",
    "language_default",
  ])
  const requiredIdentityStrings = ["project_root", "smell", "location"] as const
  const optionalIdentityStrings = [
    "project_override_root",
    "language",
    "target_context_json",
    "sample_test_location",
    "sample_test_command",
    "build_command",
    "project_test_command",
    "verification_cwd",
    "verification_command_source",
    "sample_test_source",
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
  const buildCommand = String(identity.build_command || "").trim()
  const projectTestCommand = String(identity.project_test_command || "").trim()
  const verificationCwd = String(identity.verification_cwd || "").trim()
  const verificationCommandSource = String(identity.verification_command_source || "").trim()
  const sampleTestCommand = String(identity.sample_test_command || "").trim()
  const sampleTestSource = String(identity.sample_test_source || "").trim()
  if (Boolean(buildCommand) !== Boolean(projectTestCommand) || (verificationCwd && !buildCommand)) {
    throw new Error(
      "EXPLICIT_VERIFICATION_COMMAND_PAIR_REQUIRED: build_command and "
      + "project_test_command must be provided together before verification_cwd",
    )
  }
  if (verificationCommandSource && !buildCommand) {
    throw new Error(
      "VERIFICATION_COMMAND_SOURCE_WITHOUT_COMMANDS: "
      + "verification_command_source requires the complete build/project-test pair",
    )
  }
  if (sampleTestSource && !sampleTestCommand) {
    throw new Error(
      "SAMPLE_TEST_SOURCE_WITHOUT_COMMAND: sample_test_source requires sample_test_command",
    )
  }
  for (const [field, source] of [
    ["verification_command_source", verificationCommandSource],
    ["sample_test_source", sampleTestSource],
  ] as const) {
    if (source && !allowedCommandSources.has(source)) {
      throw new Error(`INVALID_COMMAND_TASK_IDENTITY: unsupported ${field} '${source}'`)
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
    identity: Object.fromEntries(
      COMMAND_IDENTITY_FIELDS.map(([key]) => [key, identity[key]]),
    ) as CommandTaskIdentity,
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
  const metricBudget = checkpointMetricBudget(plan)
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
  if (metricBudget.length) {
    lines.push("- Immutable numeric edit budget (bounded baseline planning input):")
    for (const item of metricBudget) {
      lines.push(
        `  - metric=${item.metric}, current=${item.current}, ${item.boundaryKey}=${item.boundary}, required_reduction=${item.requiredReduction}, unit=${item.unit}`,
      )
    }
    lines.push("- This budget is necessary planning information, not acceptance authority; final acceptance is still decided by frozen target identity, semantic closure, and the controller-owned configured build/test verification.")
    lines.push("- After one coherent production edit, call smell_verify. While the source Guard is still above the passing route, it runs only the configured isolated focused preflight and cannot accept the sample or execute project_full.")
    lines.push("- Use focused_preflight diagnostics for the next narrow correction. Do not manually run a heavy project build in the candidate source tree.")
    lines.push("- Once the source Guard crosses a passing route, the same smell_verify call advances to final acceptance under the controller-owned verification mode.")
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
    policy.checkpoint_required
      ? `smell_verify is the controller-owned staged gate under verification_mode=${policy.verification_mode}: source Guard, optional isolated focused preflight, then final acceptance only after the Guard passes.`
      : "Call smell_verify as the acceptance gate. Its loop.decision field is authoritative.",
    "When a final verification returns loop.decision=continue, read loop.instruction from that tool result before one narrow correction.",
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
      "- An unchanged baseline can never pass; make a substantive production-source refactoring.",
      "- A decreased metric is IMPROVED only. PASS requires the frozen target smell to disappear plus structural and build/test preservation.",
    )
  }
  if (isProtectedProjectFullCandidateShellPolicy(policy)) {
    lines.push(
      "",
      "Candidate source-tree tool contract:",
      "- Bash is disabled for this controller-managed project_full Python/C/C++ session.",
      "- Use read, grep, glob, or list for inspection and edit, write, patch, or apply_patch for source changes.",
      "- Call smell_verify for every compile or test; it owns the configured isolated focused preflight and final project_full verification.",
    )
  }
  if (backend === "idea") {
    lines.push(
      "",
      "IDEA refactoring backend contract:",
      "- Load the exact smell-repair-<task-smell-with-hyphens> semantic skill and idea-refactor-cli backend skill; read only the current smell's Java and IDEA routes.",
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

function buildLoopDecision(
  state: CommandLoopState,
  input: {
    decision: "continue" | "stop"
    terminationReason: string
    elapsedSeconds: number
    failureCategory: string
    failureGroup: string
    instruction: string
  },
) {
  return {
    decision: input.decision,
    termination_reason: input.terminationReason,
    continuation: state.continuationCount,
    max_continuations: state.policy.loop.max_continuations,
    cap_recovery_used: state.capRecoveryUsed,
    remaining: Math.max(0, state.policy.loop.max_continuations - state.continuationCount),
    no_progress_count: state.noProgressCount,
    no_progress_limit: state.policy.loop.no_progress_limit,
    elapsed_seconds: input.elapsedSeconds,
    sample_deadline_seconds: state.policy.loop.sample_deadline_seconds,
    failure_category: input.failureCategory,
    failure_group: input.failureGroup,
    instruction: input.instruction,
  }
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

function applyCommandLoopDecision(
  normalized: { output: string; metadata: Record<string, unknown> },
  state: CommandLoopState,
): PreparedLoopOutput | null {
  let payload: Record<string, unknown>
  try {
    payload = JSON.parse(normalized.output) as Record<string, unknown>
  } catch {
    return null
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
  const pack = payload.failure_pack && typeof payload.failure_pack === "object" && !Array.isArray(payload.failure_pack)
    ? payload.failure_pack as Record<string, unknown>
    : null
  const packCategory = pack?.failure_category
  const category = pack ? String(packCategory || "") : ""
  const group = pack ? String(pack.failure_group || "") : ""
  const bridgeRetryable = pack?.retryable === true
  const nextAction = typeof pack?.next_action === "string"
    ? pack.next_action.trim()
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

  const loop = buildLoopDecision(state, {
    decision,
    terminationReason,
    elapsedSeconds,
    failureCategory: category,
    failureGroup: group,
    instruction: decision === "continue"
      ? (nextAction
          ? nextAction
          : improvedOnly && typeof payload.continue_hint === "string" && payload.continue_hint
          ? payload.continue_hint
          : state.policy.loop.instruction)
      : "",
  })
  payload.loop = loop
  normalized.output = safeJsonStringify(payload)
  normalized.metadata.loop = toJsonSafe(loop)
  return {
    payload,
    failureCategory: typeof packCategory === "string" ? packCategory.trim() : "",
  }
}

function applyGuardProgressDecision(
  normalized: { output: string; metadata: Record<string, unknown> },
  state: CommandLoopState,
) {
  let payload: Record<string, unknown>
  try {
    payload = JSON.parse(normalized.output) as Record<string, unknown>
  } catch {
    return
  }
  const identity = Object.fromEntries(
    Object.entries(state.policy.identity).sort(([left], [right]) => left.localeCompare(right)),
  )
  const focused = recordValue(payload.focused_preflight)
  const focusedExecution = recordValue(focused?.execution)
  const fingerprint = "guard-progress:" + createHash("sha256")
    .update(JSON.stringify({
      identity,
      metric_budget: toJsonSafe(payload.metric_budget),
      focused_preflight: focused
        ? {
            status: focused.status,
            returncode: focusedExecution?.returncode,
            summary_text: focused.status === "FAILED"
              ? focusedExecution?.summary_text
              : "",
          }
        : null,
    }))
    .digest("hex")
  if (state.lastFailureFingerprint && state.lastFailureFingerprint === fingerprint) {
    state.noProgressCount += 1
  } else {
    state.noProgressCount = 0
  }
  state.lastFailureFingerprint = fingerprint
  const elapsedSeconds = Math.max(0, Math.floor((Date.now() - state.startedAt) / 1000))
  let decision: "continue" | "stop" = "continue"
  let terminationReason = ""
  if (elapsedSeconds >= state.policy.loop.sample_deadline_seconds) {
    decision = "stop"
    terminationReason = "GUARD_PROGRESS_SAMPLE_DEADLINE"
  } else if (state.noProgressCount >= state.policy.loop.no_progress_limit) {
    decision = "stop"
    terminationReason = "GUARD_PROGRESS_NO_PROGRESS"
  }
  const loop = buildLoopDecision(state, {
    decision,
    terminationReason,
    elapsedSeconds,
    failureCategory: "GUARD_PROGRESS_REQUIRED",
    failureGroup: "smell",
    instruction: decision === "continue" && typeof payload.next_action === "string"
      ? payload.next_action
      : "",
  })
  payload.loop = loop
  normalized.output = safeJsonStringify(payload)
  normalized.metadata.loop = toJsonSafe(loop)
}

export const SmellPlugin: Plugin = async ({ worktree, client }) => {
  const idleRuntime = createIdleContinueRuntime({ client, env: process.env })
  const refactoringBackend = String(process.env.SMELL_REFACTORING_BACKEND || "direct").trim().toLowerCase()
  const ideaToolsEnabled = refactoringBackend === "idea" && process.env.SMELL_ENABLE_IDEA_TOOLS === "1"
  const commandLoopStates = new Map<string, CommandLoopState>()
  const commandSessionParents = new Map<string, string>()
  const protectedShellLineage = new Set<string>()
  const commandBaselineSeals = new Map<string, string>()
  const markProtectedShellLineage = (sessionID: string): void => {
    if (!sessionID) return
    const pending = [sessionID]
    while (pending.length > 0) {
      const current = pending.pop() || ""
      if (!current || protectedShellLineage.has(current)) continue
      protectedShellLineage.add(current)
      for (const [childID, parentID] of commandSessionParents.entries()) {
        if (parentID === current) pending.push(childID)
      }
    }
  }
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
    if (isProtectedProjectFullCandidateShellSession(restored)) {
      markProtectedShellLineage(sessionID)
    }
    return restored
  }
  const hasProtectedShellAncestor = (sessionID: string): boolean => {
    const visited = new Set<string>()
    let current = commandSessionParents.get(sessionID) || ""
    while (current && !visited.has(current)) {
      visited.add(current)
      const state = commandLoopStates.get(current)
      if (
        protectedShellLineage.has(current)
        || isProtectedProjectFullCandidateShellSession(state)
      ) return true
      current = commandSessionParents.get(current) || ""
    }
    return false
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
      description: "Run the controller-owned staged Guard: source metrics, optional isolated focused preflight, and final configured build/test only after the source Guard passes.",
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
          resolved.buildCommand = controllerIdentity.buildCommand
          resolved.projectTestCommand = controllerIdentity.projectTestCommand
          resolved.verificationCwd = controllerIdentity.verificationCwd
          resolved.verificationCommandSource = controllerIdentity.verificationCommandSource
          resolved.sampleTestSource = controllerIdentity.sampleTestSource
          resolved.checkpointRequired = controllerIdentity.checkpointRequired
        }
        const javaCheckpoint = isJavaCheckpointIdentity(resolved)
        const baselineSeal = commandBaselineSeals.get(sessionID) || envDefault("SMELL_BASELINE_SEAL")
        if (javaCheckpoint && !baselineSeal) {
          throw new Error("CHECKPOINT_CONTROLLER_SEAL_MISSING: Java checkpoint verification requires the external baseline seal")
        }
        if (
          commandState
          && commandState.lastFailureFingerprint.startsWith("guard-progress:")
          && commandState.noProgressCount >= commandState.policy.loop.no_progress_limit
        ) {
          const elapsedSeconds = Math.max(
            0,
            Math.floor((Date.now() - commandState.startedAt) / 1000),
          )
          const loop = buildLoopDecision(commandState, {
            decision: "stop",
            terminationReason: "GUARD_PROGRESS_NO_PROGRESS",
            elapsedSeconds,
            failureCategory: "GUARD_PROGRESS_REQUIRED",
            failureGroup: "smell",
            instruction: "",
          })
          const payload = {
            schema_version: "smell.guard-progress/v1",
            success: false,
            status: "GUARD_PROGRESS_REQUIRED",
            applicable: true,
            checkpoint_required: true,
            source_guard_passed: false,
            ready_for_project_full: false,
            project_full_executed: false,
            next_action: "",
            loop,
          }
          const normalized = normalizeToolResult(name, {
            exitCode: 0,
            stdout: JSON.stringify(payload),
            stderr: "",
            json: payload,
          })
          normalized.metadata.loop = toJsonSafe(loop)
          normalized.metadata.command_loop_state = toJsonSafe(
            commandLoopStateSnapshot(commandState),
          )
          return normalized
        }
        if (usesCheapGuardProgressGate(resolved, commandState)) {
          const progressResult = await runBridge(worktree, [
            "verify",
            ...commonArgs({ ...resolved, baselineSeal }),
            "--guard-progress-only",
          ])
          const progressPayload = recordValue(progressResult.json)
          const progressPassed = Boolean(
            progressResult.exitCode === 0
            && progressPayload?.schema_version === "smell.guard-progress/v1"
            && progressPayload?.success === true
            && progressPayload?.status === "GUARD_PROGRESS_PASSED"
            && progressPayload?.applicable === true
            && progressPayload?.source_guard_passed === true
            && progressPayload?.ready_for_project_full === true
            && progressPayload?.project_full_executed === false
          )
          if (!progressPassed) {
            const progressRequired = Boolean(
              progressPayload?.schema_version === "smell.guard-progress/v1"
              && progressPayload?.success === false
              && progressPayload?.status === "GUARD_PROGRESS_REQUIRED"
              && progressPayload?.applicable === true
              && progressPayload?.checkpoint_required === true
              && progressPayload?.source_guard_passed === false
              && progressPayload?.ready_for_project_full === false
              && progressPayload?.project_full_executed === false
            )
            let renderedProgressResult = progressResult
            if (progressRequired && progressPayload) {
              const focusedResult = await runBridge(worktree, [
                "verify",
                ...commonArgs({ ...resolved, baselineSeal }),
                "--focused-preflight-only",
              ])
              const focusedPayload = recordValue(focusedResult.json)
              const focusedValid = Boolean(
                focusedResult.exitCode === 0
                && focusedPayload?.type === "focused_preflight"
                && focusedPayload?.acceptance === false
                && focusedPayload?.project_full_executed === false
                && ["NOT_APPLICABLE", "READY", "FAILED"].includes(
                  String(focusedPayload?.status || ""),
                )
              )
              if (!focusedValid) {
                const normalized = normalizeToolResult(name, focusedResult)
                if (commandState) {
                  applyCommandLoopDecision(normalized, commandState)
                  normalized.metadata.command_loop_state = toJsonSafe(
                    commandLoopStateSnapshot(commandState),
                  )
                }
                return normalized
              }
              progressPayload.focused_preflight = focusedPayload
              progressPayload.focused_preflight_executed = (
                focusedPayload?.status !== "NOT_APPLICABLE"
              )
              if (focusedPayload?.status === "FAILED") {
                const execution = recordValue(focusedPayload.execution)
                const diagnostic = typeof execution?.summary_text === "string"
                  ? execution.summary_text
                  : typeof focusedPayload?.message === "string"
                  ? focusedPayload.message
                  : "Focused preflight failed."
                progressPayload.next_action = `Repair the focused preflight failure: ${diagnostic}`
              }
              renderedProgressResult = {
                ...progressResult,
                stdout: JSON.stringify(progressPayload),
                stderr: [progressResult.stderr, focusedResult.stderr].filter(Boolean).join("\n"),
                json: progressPayload,
              }
            }
            const normalized = normalizeToolResult(name, renderedProgressResult)
            if (commandState) {
              if (progressRequired) applyGuardProgressDecision(normalized, commandState)
              normalized.metadata.command_loop_state = toJsonSafe(
                commandLoopStateSnapshot(commandState),
              )
            }
            return normalized
          }
        }
        const bridgeArgs = ["verify", ...commonArgs({ ...resolved, baselineSeal }), "--output-detail", "decision"]
        if (args.noSnapshot) bridgeArgs.push("--no-snapshot")
        const normalized = normalizeToolResult(name, await runBridge(worktree, bridgeArgs))
        let preparedOutput: PreparedLoopOutput | null | undefined
        if (commandState) {
          preparedOutput = applyCommandLoopDecision(normalized, commandState)
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
            preparedOutput,
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
      const commandState = sessionID
        ? (commandLoopStates.get(sessionID) || restoreBatchCommandState(sessionID))
        : undefined
      const inheritedShellProtection = Boolean(
        sessionID
        && (
          protectedShellLineage.has(sessionID)
          || hasProtectedShellAncestor(sessionID)
        )
      )
      if (inheritedShellProtection) markProtectedShellLineage(sessionID)
      if (
        isProtectedProjectFullCandidateShellSession(commandState)
        || inheritedShellProtection
      ) {
        throw new Error(
          "SMELL_CANDIDATE_SHELL_FORBIDDEN: controller-managed project_full "
          + "Python/C/C++ sessions do not execute bash in the candidate source tree. "
          + "Use read, grep, glob, or list for inspection; use edit, write, patch, or "
          + "apply_patch for source changes; call smell_verify for every compile or test. "
          + "smell_verify runs configured focused and full verification in a disposable worktree.",
        )
      }
      const buildJobLimit = controllerBuildJobLimit()
      if (buildJobLimit && isCappedCheckpointBuildSession(commandState)) {
        const violation = explicitBuildParallelismViolation(command, buildJobLimit)
        if (violation) {
          throw new Error(
            `SMELL_BUILD_PARALLELISM_EXCEEDED: ${violation.tool} requested ${violation.requested}; `
            + `the controller limit is ${buildJobLimit}. Use no concurrency flag, a value at or below `
            + "SMELL_BUILD_JOBS, or ${SMELL_BUILD_JOBS:-1}.",
          )
        }
      }
      if (sessionID && commandState && DIRECT_BUILD_COMMAND_RE.test(command)) {
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
            buildCommand: identity.buildCommand,
            projectTestCommand: identity.projectTestCommand,
            verificationCwd: identity.verificationCwd,
            verificationCommandSource: identity.verificationCommandSource,
            sampleTestSource: identity.sampleTestSource,
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
      const commandState = newCommandLoopState(policy, targetIdentityPrompt)
      commandLoopStates.set(input.sessionID, commandState)
      if (isProtectedProjectFullCandidateShellSession(commandState)) {
        markProtectedShellLineage(input.sessionID)
      }
      idleRuntime.clearSession(input.sessionID)
      idleRuntime.armInitialVerification({
        sessionID: input.sessionID,
        agent:
          input.command === "smell-refactor-run"
            ? "smell-refactor-agent"
            : "java-refactor-agent",
        directory: worktree,
        maxContinuations: policy.loop.max_continuations,
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
        if (event.type === "session.created") {
          const properties = (event as {
            properties?: {
              sessionID?: string
              info?: { id?: string; parentID?: string }
            }
          }).properties
          const sessionID = properties?.sessionID || properties?.info?.id || ""
          const parentID = properties?.info?.parentID || ""
          if (sessionID && parentID) {
            commandSessionParents.set(sessionID, parentID)
            if (
              protectedShellLineage.has(parentID)
              || isProtectedProjectFullCandidateShellSession(
                commandLoopStates.get(parentID),
              )
              || hasProtectedShellAncestor(parentID)
            ) {
              markProtectedShellLineage(sessionID)
            }
          }
          return
        }
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
            commandSessionParents.delete(sessionID)
            protectedShellLineage.delete(sessionID)
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
      commandSessionParents.clear()
      protectedShellLineage.clear()
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
  renderIdeaResult,
  renderIdeaApplyProtocolResult,
  runIdeaPreviewProtocol,
  ideaDecisionsShape,
  checkpointTargetIdentityPrompt,
  commandControllerSystemContext,
  commandLoopStateSnapshot,
  restoreCommandLoopState,
  isJavaCheckpointIdentity,
  usesCheapGuardProgressGate,
  applyCommandLoopDecision,
  MAX_STDOUT_STDERR_LEN,
  // Idle continuation helpers exercised by the harness:
  makeTaskKey,
  buildContinuationMessage,
  buildVerifyRequiredMessage,
  createIdleContinueRuntime,
  shouldPluginHandleSessionIdle,
  SMELL_IDLE_CONTINUE_PREFIX,
}

export default SmellPlugin

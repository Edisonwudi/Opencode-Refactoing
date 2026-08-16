import { spawn } from "node:child_process"
import { createHash } from "node:crypto"
import { fileURLToPath } from "node:url"
import { homedir } from "node:os"
import path from "node:path"
import {
  chmodSync,
  existsSync,
  mkdirSync,
  readFileSync,
  readdirSync,
  renameSync,
  unlinkSync,
  writeFileSync,
} from "node:fs"
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
  max_smell_verify_cycles: number
  no_progress_limit: number
  allowed_failure_groups: string[]
  instruction: string
  sample_deadline_seconds: number
}

type VerificationMode = "local" | "auto" | "sample_optimized" | "project_full"
type RefactoringBackend = "direct" | "idea"

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
  refactoring_backend: RefactoringBackend
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
  control: CommandControlState
  smellVerifyCycleCount: number
  noProgressCount: number
  lastFailureFingerprint: string
  bestMetricDeficit: number | null
  bestStructuralFailureCount: number | null
  lastBlockerCodes: string[]
  seenStructuralStates: string[]
  formalCandidateState: FormalCandidateState
  ideaProtocolState: IdeaProtocolState
  terminalReceipt: CommandTerminalReceipt | null
}

type IdeaActiveProposal = {
  proposalId: string
  operation: string
  status: "ready" | "needs_input" | "needs_decision" | "retryable_failed"
}

type IdeaProposalBlocker = {
  status: "unsupported_target"
  proposalId: string
  operation: string
  diagnosticCodes: string[]
}

type IdeaProtocolState = {
  activeProposal: IdeaActiveProposal | null
  proposalBlocker: IdeaProposalBlocker | null
  mutationGeneration: number
  verifiedGeneration: number
  mutationRoute: "" | "native_apply" | "authorized_edit" | "apply_outcome_unknown"
  mutationProposalId: string
  revertibleApplyGeneration: number | null
}

type FormalCandidateIdentity = {
  baselineRevision: string
  baselineTree: string
  productionDiff: string
  testTree: string
  verificationConfigTree: string
}

type FormalCandidateState = {
  candidateIdentity: FormalCandidateIdentity | null
  outcome: "" | "pass" | "test_failed" | "failed"
  diagnosticSignature: string
  confirmationRequired: boolean
}

type CommandSessionMetadata = {
  command: "smell-refactor-run" | "java-refactor-run"
  agent: "smell-refactor-agent" | "java-refactor-agent"
  initialization: "baseline_pending" | "ready"
}

type CommandControlState = {
  generation: number
  decision: "verify_required" | "continue" | "stop"
  instruction: string
  terminationReason: string
}

type CommandTerminalReceipt = {
  stage: "cheap_guard" | "formal_verify" | "protocol"
  status: string
  success: boolean
  accepted: boolean
  resolution: string
  terminationReason: string
  failureCategory: string
  failureGroup: string
  formalVerificationReceipt: Record<string, unknown> | null
  ideaProtocolReceipt: Record<string, unknown> | null
  loop: Record<string, unknown>
}

const COMMAND_LOOP_STATE_VERSION = 7
const COMMAND_LOOP_STATE_ENV = "SMELL_COMMAND_LOOP_STATE_JSON"
const BASELINE_CONTEXT_FILE_ENV = "SMELL_BASELINE_CONTEXT_FILE"
const CONTROLLER_CONTEXT_AUDIT_FILE_ENV = "SMELL_CONTROLLER_CONTEXT_AUDIT_FILE"
const COMMAND_SESSION_STATE_ROOT_ENV = "SMELL_SESSION_STATE_ROOT"
const COMMAND_SESSION_STATE_SCHEMA = "smell.session-command-state/v1"
const COMMAND_SESSION_LINEAGE_SCHEMA = "smell.session-lineage/v1"
const FORMAL_VERIFICATION_RECEIPT_SCHEMA = "smell.formal-verification-receipt/v1"
const IDEA_PROTOCOL_RECEIPT_SCHEMA = "smell.idea-protocol-receipt/v1"
const INITIAL_VERIFY_INSTRUCTION = "Call smell_verify now using the frozen command identity."
const FRESH_CONFIRMATION_INSTRUCTION = "Do not edit the candidate; call smell_verify again for one fresh confirmation."
const DEADLINE_EXIT_CODE = 124
const DEADLINE_TERM_GRACE_MS = 1000
const DEADLINE_KILL_GRACE_MS = 500
const COMMAND_RESOLUTION_DEADLINE_MS = 60_000
const MAX_SEEN_STRUCTURAL_STATES = 32

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
  maxSmellVerifyCycles: number
  pending: boolean
  dispatching: boolean
  awaitingVerify: boolean
  awaitingVerifyReason: "initial" | "continuation"
  verifyReminderGeneration: number
  agent: string
  directory: string
  failureCategory: string
  instruction: string
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
    `${SMELL_IDLE_CONTINUE_PREFIX} ${state.continuation}/${state.maxSmellVerifyCycles}]`,
    "Resume the existing task in this session.",
    state.instruction,
  ].join("\n")
}

function buildVerifyRequiredMessage(state: ContinuationState): string {
  return [
    `${SMELL_IDLE_CONTINUE_PREFIX} verify-required/${state.awaitingVerifyReason}/${state.generation}]`,
    "Resume the existing task in this session and call smell_verify now.",
    state.instruction,
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

type BoundedProcessResult = {
  exitCode: number
  stdout: string
  stderr: string
  timedOut: boolean
}

function terminateSpawnedProcess(
  child: ReturnType<typeof spawn>,
  signal: NodeJS.Signals,
): void {
  if (child.pid && process.platform !== "win32") {
    try {
      process.kill(-child.pid, signal)
      return
    } catch {
      // The child may have exited between the deadline check and the signal.
    }
  }
  try {
    child.kill(signal)
  } catch {
    // Final settlement is bounded independently of signal delivery.
  }
}

async function runDeadlineBoundProcess(input: {
  executable: string
  args: string[]
  cwd: string
  deadlineEpochMs?: number
}): Promise<BoundedProcessResult> {
  if (input.deadlineEpochMs !== undefined && input.deadlineEpochMs <= Date.now()) {
    return {
      exitCode: DEADLINE_EXIT_CODE,
      stdout: "",
      stderr: "SAMPLE_DEADLINE_REACHED: command was not started after the plugin-owned deadline",
      timedOut: true,
    }
  }
  return await new Promise((resolve) => {
    let stdout = ""
    let stderr = ""
    let settled = false
    let timedOut = false
    let deadlineTimer: NodeJS.Timeout | undefined
    let killTimer: NodeJS.Timeout | undefined
    let settleTimer: NodeJS.Timeout | undefined
    const finalize = (exitCode: number) => {
      if (settled) return
      settled = true
      if (deadlineTimer) clearTimeout(deadlineTimer)
      if (killTimer) clearTimeout(killTimer)
      if (settleTimer) clearTimeout(settleTimer)
      resolve({ exitCode: timedOut ? DEADLINE_EXIT_CODE : exitCode, stdout, stderr, timedOut })
    }
    const childEnv = { ...process.env }
    if (input.deadlineEpochMs !== undefined) {
      childEnv.SMELL_SAMPLE_DEADLINE_EPOCH_MS = String(Math.trunc(input.deadlineEpochMs))
    }
    const child = spawn(input.executable, input.args, {
      cwd: input.cwd,
      env: childEnv,
      stdio: ["ignore", "pipe", "pipe"],
      detached: process.platform !== "win32",
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
    if (input.deadlineEpochMs !== undefined) {
      const remainingMs = Math.max(0, input.deadlineEpochMs - Date.now())
      deadlineTimer = setTimeout(() => {
        timedOut = true
        stderr = stderr || "SAMPLE_DEADLINE_REACHED: command exceeded the plugin-owned deadline"
        terminateSpawnedProcess(child, "SIGTERM")
        killTimer = setTimeout(() => {
          terminateSpawnedProcess(child, "SIGKILL")
        }, DEADLINE_TERM_GRACE_MS)
        settleTimer = setTimeout(() => {
          finalize(DEADLINE_EXIT_CODE)
        }, DEADLINE_TERM_GRACE_MS + DEADLINE_KILL_GRACE_MS)
      }, remainingMs)
    }
  })
}

async function runBridge(
  worktree: string,
  args: string[],
  deadlineEpochMs?: number,
): Promise<BridgeResult> {
  const result = await runDeadlineBoundProcess({
    executable: "python3",
    args: [bridgeFile, ...args],
    cwd: worktree,
    deadlineEpochMs,
  })
  if (result.timedOut) {
    return {
      ...result,
      json: {
        success: false,
        accepted: false,
        status: "SAMPLE_DEADLINE_REACHED",
        resolution: "rejected",
      },
    }
  }
  let json: unknown = null
  try {
    json = JSON.parse(result.stdout)
  } catch {
    json = null
  }
  return { ...result, json }
}

async function runIdeaCli(
  worktree: string,
  cli: string,
  args: string[],
  deadlineEpochMs?: number,
): Promise<IdeaCliResult> {
  const result = await runDeadlineBoundProcess({
    executable: cli,
    args,
    cwd: worktree,
    deadlineEpochMs,
  })
  let json: unknown
  if (result.timedOut) {
    json = {
      status: "failed",
      diagnostics: [{ code: "SAMPLE_DEADLINE_REACHED", summary: "IDEA command exceeded the plugin-owned deadline." }],
    }
  } else {
    try {
      json = JSON.parse(result.stdout)
    } catch {
      json = {
        status: "failed",
        diagnostics: [{ code: "IDEA_CLI_OUTPUT_PARSE_FAILED", summary: "IDEA CLI output was not valid JSON." }],
        stdout: result.stdout,
      }
    }
  }
  return { ...result, json, argv: args }
}

function resolveIdeaInput(input: {
  projectRoot?: string
  ideaProjectRoot?: string
  ideaRefactorCli?: string
  language?: string
} = {}) {
  const language = input.language || envDefault("SMELL_LANGUAGE")
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

function resolveIdeaFile(file: string, resolvedProjectRoot: string, allowMissing: boolean = false) {
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
  const normalizedProjectRoot = path.resolve(resolvedProjectRoot)
  const ideaCandidate = path.resolve(normalizedProjectRoot, rawFile)
  const relative = path.relative(normalizedProjectRoot, ideaCandidate)
  if (relative === ".." || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative)) {
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
                code: "IDEA_FILE_OUTSIDE_PROJECT_ROOT",
                summary: `File '${rawFile}' is outside the frozen IDEA project root.`,
              },
            ],
          },
          null,
          2,
        ),
        metadata: { exitCode: 1, stderr: "" },
      },
    }
  }
  if (allowMissing || existsSync(ideaCandidate)) {
    return { ok: true as const, file: ideaCandidate }
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
              summary: `Unable to resolve '${rawFile}' under the frozen IDEA root '${normalizedProjectRoot}'.`,
            },
          ],
          attempted_paths: [ideaCandidate],
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

function newIdeaProtocolState(): IdeaProtocolState {
  return {
    activeProposal: null,
    proposalBlocker: null,
    mutationGeneration: 0,
    verifiedGeneration: 0,
    mutationRoute: "",
    mutationProposalId: "",
    revertibleApplyGeneration: null,
  }
}

function assertIdeaPreviewAllowed(
  state: IdeaProtocolState,
  request: { operation?: string; proposalId?: string },
): void {
  const proposalId = String(request.proposalId || "").trim()
  if (!proposalId) return
  const active = state.activeProposal
  if (!active || active.proposalId !== proposalId) {
    throw new Error("IDEA_PROPOSAL_ID_MISMATCH: preview continuation must use the active proposalId")
  }
  if (String(request.operation || "").trim() !== active.operation) {
    throw new Error("IDEA_PROPOSAL_OPERATION_MISMATCH: preview continuation cannot change operation")
  }
}

function recordIdeaPreviewOutcome(
  state: IdeaProtocolState,
  request: { operation?: string; proposalId?: string },
  payload: Record<string, unknown>,
): void {
  assertIdeaPreviewAllowed(state, request)
  const operation = String(payload.operation || request.operation || "").trim()
  const proposalId = String(payload.proposalId || "").trim()
  const requestProposalId = String(request.proposalId || "").trim()
  const status = String(payload.status || "").trim()
  if (payload.protocol !== "idea-proposal-v1" || !operation || !status) {
    throw new Error("IDEA_PREVIEW_PROTOCOL_INVALID: preview returned an incomplete protocol result")
  }
  if (requestProposalId && proposalId !== requestProposalId) {
    throw new Error("IDEA_PROPOSAL_ID_MISMATCH: preview result changed the active proposalId")
  }
  if (requestProposalId && state.activeProposal?.operation !== operation) {
    throw new Error("IDEA_PROPOSAL_OPERATION_MISMATCH: preview result changed operation")
  }

  if (["ready", "needs_input", "needs_decision", "retryable_failed"].includes(status) && proposalId) {
    state.activeProposal = {
      proposalId,
      operation,
      status: status as IdeaActiveProposal["status"],
    }
  } else {
    state.activeProposal = null
  }
  if (status === "unsupported_target") {
    state.proposalBlocker = {
      status: "unsupported_target",
      proposalId,
      operation,
      diagnosticCodes: diagnosticCodes(payload).slice(0, 8),
    }
  } else {
    state.proposalBlocker = null
  }
}

function assertIdeaApplyAllowed(state: IdeaProtocolState, proposalId: string): void {
  const active = state.activeProposal
  if (!active || active.status !== "ready") {
    throw new Error("IDEA_APPLY_REQUIRES_READY_PROPOSAL: call preview until the active proposal is ready")
  }
  if (String(proposalId || "").trim() !== active.proposalId) {
    throw new Error("IDEA_PROPOSAL_ID_MISMATCH: apply must use the active proposalId")
  }
}

function recordIdeaApplyOutcome(
  state: IdeaProtocolState,
  proposalId: string,
  payload: Record<string, unknown>,
): void {
  assertIdeaApplyAllowed(state, proposalId)
  const active = state.activeProposal as IdeaActiveProposal
  const resultProposalId = String(payload.proposalId || "").trim()
  const operation = String(payload.operation || active.operation).trim()
  const status = String(payload.status || "").trim()
  if (
    payload.protocol !== "idea-proposal-v1"
    || resultProposalId !== active.proposalId
    || operation !== active.operation
    || !status
  ) {
    throw new Error("IDEA_APPLY_PROTOCOL_INVALID: apply result does not match the active proposal")
  }
  if (status === "applied" || status === "outcome_unknown") {
    state.mutationGeneration += 1
    state.mutationRoute = status === "applied" ? "native_apply" : "apply_outcome_unknown"
    state.mutationProposalId = active.proposalId
    state.revertibleApplyGeneration = status === "applied" ? state.mutationGeneration : null
    state.activeProposal = null
    state.proposalBlocker = null
    return
  }
  if (["needs_input", "needs_decision", "retryable_failed"].includes(status)) {
    state.activeProposal = { ...active, status: "ready" }
    return
  }
  state.activeProposal = null
  state.proposalBlocker = null
}

function assertIdeaEditAllowed(state: IdeaProtocolState): void {
  if (state.proposalBlocker?.status !== "unsupported_target") {
    throw new Error(
      "IDEA_EDIT_REQUIRES_UNSUPPORTED_TARGET: idea_edit requires an explicit unsupported_target preview blocker",
    )
  }
}

function recordIdeaEditOutcome(state: IdeaProtocolState, payload: Record<string, unknown>): void {
  assertIdeaEditAllowed(state)
  if (payload.success !== true) return
  state.mutationGeneration += 1
  state.mutationRoute = "authorized_edit"
  state.mutationProposalId = state.proposalBlocker?.proposalId || ""
  state.revertibleApplyGeneration = null
}

function assertIdeaVerifyAllowed(
  state: IdeaProtocolState,
  input: { controlGeneration: number; confirmationRequired: boolean },
): void {
  if (state.activeProposal) {
    throw new Error("IDEA_VERIFY_REQUIRES_APPLY: resolve and apply the active proposal before verification")
  }
  if (state.mutationGeneration === 0) {
    if (input.controlGeneration === 0 && !state.proposalBlocker) return
    throw new Error("IDEA_VERIFY_REQUIRES_MUTATION: no command-owned IDEA mutation is pending verification")
  }
  if (
    state.mutationGeneration === state.verifiedGeneration
    && !input.confirmationRequired
  ) {
    throw new Error("IDEA_VERIFY_REQUIRES_NEW_MUTATION: the latest IDEA mutation was already verified")
  }
}

function recordIdeaVerifyOutcome(state: IdeaProtocolState): void {
  if (state.mutationGeneration > 0) {
    state.verifiedGeneration = state.mutationGeneration
    state.revertibleApplyGeneration = null
  }
}

function assertIdeaRevertAllowed(state: IdeaProtocolState): void {
  if (
    state.revertibleApplyGeneration === null
    || state.revertibleApplyGeneration !== state.mutationGeneration
    || state.verifiedGeneration >= state.mutationGeneration
  ) {
    throw new Error("IDEA_REVERT_REQUIRES_COMMAND_APPLY: no unverified command-owned IDEA apply can be reverted")
  }
}

function recordIdeaRevertOutcome(state: IdeaProtocolState, payload: Record<string, unknown>): void {
  assertIdeaRevertAllowed(state)
  if (payload.success !== true) return
  Object.assign(state, newIdeaProtocolState())
}

function ideaProtocolReceipt(state: IdeaProtocolState): Record<string, unknown> {
  return {
    schema_version: IDEA_PROTOCOL_RECEIPT_SCHEMA,
    mutation_generation: state.mutationGeneration,
    verified_generation: state.verifiedGeneration,
    mutation_route: state.mutationRoute,
    proposal_id: state.mutationProposalId,
    blocker_status: state.proposalBlocker?.status || "",
    blocker_codes: state.proposalBlocker?.diagnosticCodes || [],
    complete: state.mutationGeneration > 0
      && state.mutationGeneration === state.verifiedGeneration,
  }
}

function ideaProtocolReceiptMatchesState(
  value: Record<string, unknown>,
  state: IdeaProtocolState,
): boolean {
  const blockerCodes = state.proposalBlocker?.diagnosticCodes || []
  return Boolean(
    value.schema_version === IDEA_PROTOCOL_RECEIPT_SCHEMA
    && value.mutation_generation === state.mutationGeneration
    && value.verified_generation === state.verifiedGeneration
    && value.mutation_route === state.mutationRoute
    && value.proposal_id === state.mutationProposalId
    && value.blocker_status === (state.proposalBlocker?.status || "")
    && Array.isArray(value.blocker_codes)
    && value.blocker_codes.length === blockerCodes.length
    && value.blocker_codes.every((item, index) => item === blockerCodes[index])
    && value.complete === (
      state.mutationGeneration > 0
      && state.mutationGeneration === state.verifiedGeneration
    )
  )
}

function ideaProtocolStateSnapshot(state: IdeaProtocolState): Record<string, unknown> {
  return {
    active_proposal: state.activeProposal
      ? {
          proposal_id: state.activeProposal.proposalId,
          operation: state.activeProposal.operation,
          status: state.activeProposal.status,
        }
      : null,
    proposal_blocker: state.proposalBlocker
      ? {
          status: state.proposalBlocker.status,
          proposal_id: state.proposalBlocker.proposalId,
          operation: state.proposalBlocker.operation,
          diagnostic_codes: state.proposalBlocker.diagnosticCodes,
        }
      : null,
    mutation_generation: state.mutationGeneration,
    verified_generation: state.verifiedGeneration,
    mutation_route: state.mutationRoute,
    mutation_proposal_id: state.mutationProposalId,
    revertible_apply_generation: state.revertibleApplyGeneration,
  }
}

function restoreIdeaProtocolState(value: unknown): IdeaProtocolState | undefined {
  const raw = recordValue(value)
  if (!raw) return undefined
  const active = raw.active_proposal === null ? null : recordValue(raw.active_proposal)
  const blocker = raw.proposal_blocker === null ? null : recordValue(raw.proposal_blocker)
  const mutationGeneration = Number(raw.mutation_generation)
  const verifiedGeneration = Number(raw.verified_generation)
  const mutationRoute = String(raw.mutation_route || "")
  const mutationProposalId = raw.mutation_proposal_id
  const revertible = raw.revertible_apply_generation === null
    ? null
    : Number(raw.revertible_apply_generation)
  const activeStatus = active?.status
  const blockerCodes = blocker ? asStringArray(blocker.diagnostic_codes) : []
  if (
    !Number.isInteger(mutationGeneration)
    || mutationGeneration < 0
    || !Number.isInteger(verifiedGeneration)
    || verifiedGeneration < 0
    || verifiedGeneration > mutationGeneration
    || !["", "native_apply", "authorized_edit", "apply_outcome_unknown"].includes(mutationRoute)
    || typeof mutationProposalId !== "string"
    || (mutationGeneration === 0 && (verifiedGeneration !== 0 || mutationRoute || mutationProposalId || revertible !== null))
    || (mutationGeneration > 0 && !mutationRoute)
    || (revertible !== null && (
      !Number.isInteger(revertible)
      || revertible !== mutationGeneration
      || mutationRoute !== "native_apply"
      || verifiedGeneration >= mutationGeneration
    ))
    || (active !== null && (
      typeof active.proposal_id !== "string"
      || !active.proposal_id
      || typeof active.operation !== "string"
      || !active.operation
      || !["ready", "needs_input", "needs_decision", "retryable_failed"].includes(String(activeStatus || ""))
    ))
    || (blocker !== null && (
      blocker.status !== "unsupported_target"
      || typeof blocker.proposal_id !== "string"
      || typeof blocker.operation !== "string"
      || !blocker.operation
      || !Array.isArray(blocker.diagnostic_codes)
      || blockerCodes.length !== blocker.diagnostic_codes.length
      || blockerCodes.length > 8
    ))
    || (active !== null && blocker !== null)
  ) return undefined
  return {
    activeProposal: active
      ? {
          proposalId: String(active.proposal_id),
          operation: String(active.operation),
          status: String(activeStatus) as IdeaActiveProposal["status"],
        }
      : null,
    proposalBlocker: blocker
      ? {
          status: "unsupported_target",
          proposalId: String(blocker.proposal_id),
          operation: String(blocker.operation),
          diagnosticCodes: blockerCodes,
        }
      : null,
    mutationGeneration,
    verifiedGeneration,
    mutationRoute: mutationRoute as IdeaProtocolState["mutationRoute"],
    mutationProposalId,
    revertibleApplyGeneration: revertible,
  }
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
      payloadStatus === "unsupported_target"
        ? "unsupported_target"
        : activeResult?.exitCode === 3 && payloadStatus === "retryable_failed"
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
    maxSmellVerifyCycles: number
    generation?: number
    instruction?: string
  }) {
    if (!sessionIdleEnabled) return
    if (!input.sessionID) return
    states.set(input.sessionID, {
      taskKey: "",
      generation: input.generation ?? 0,
      dispatchedGeneration: -1,
      continuation: 0,
      maxSmellVerifyCycles: input.maxSmellVerifyCycles,
      pending: false,
      dispatching: false,
      awaitingVerify: true,
      awaitingVerifyReason: "initial",
      verifyReminderGeneration: -1,
      agent: input.agent,
      directory: input.directory,
      failureCategory: "",
      instruction: input.instruction || INITIAL_VERIFY_INSTRUCTION,
      updatedAt: Date.now(),
    })
  }

  function rehydrateFromControl(input: {
    sessionID: string
    agent: string
    directory: string
    taskKey: string
    generation: number
    decision: CommandControlState["decision"]
    instruction: string
    continuation: number
    maxSmellVerifyCycles: number
  }): void {
    if (!sessionIdleEnabled || !input.sessionID) return
    if (input.decision === "stop") {
      clearSession(input.sessionID)
      return
    }
    const existing = states.get(input.sessionID)
    if (existing && existing.generation === input.generation) return
    if (input.decision === "verify_required") {
      armInitialVerification({
        sessionID: input.sessionID,
        agent: input.agent,
        directory: input.directory,
        maxSmellVerifyCycles: input.maxSmellVerifyCycles,
        generation: input.generation,
        instruction: input.instruction,
      })
      return
    }
    states.set(input.sessionID, {
      taskKey: input.taskKey,
      generation: input.generation,
      dispatchedGeneration: -1,
      continuation: input.continuation,
      maxSmellVerifyCycles: input.maxSmellVerifyCycles,
      pending: true,
      dispatching: false,
      awaitingVerify: false,
      awaitingVerifyReason: "continuation",
      verifyReminderGeneration: -1,
      agent: input.agent,
      directory: input.directory,
      failureCategory: "",
      instruction: input.instruction,
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
    maxSmellVerifyCycles: number
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
    const maxSmellVerifyCycles = typeof loop?.max_smell_verify_cycles === "number" ? loop.max_smell_verify_cycles : 0
    const decision = typeof loop?.decision === "string" ? loop.decision : "stop"
    const controlGeneration = Number(loop?.generation)
    const instruction = typeof loop?.instruction === "string" ? loop.instruction.trim() : ""

    const base = {
      enabled: sessionIdleEnabled,
      continuation,
      maxSmellVerifyCycles,
      generation: Number.isInteger(controlGeneration)
        ? controlGeneration
        : existing ? existing.generation : 0,
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

    if (
      !preparedOutput
      || decision !== "continue"
      || continuation <= 0
      || continuation > maxSmellVerifyCycles
      || !Number.isInteger(controlGeneration)
      || controlGeneration !== (existing?.generation ?? 0) + 1
      || !instruction
    ) {
      revokePending()
      if (
        existing
        && Number.isInteger(controlGeneration)
        && controlGeneration === existing.generation + 1
      ) {
        existing.generation = controlGeneration
      }
      return { ...base, dispatched: false }
    }

    // applyCommandLoopDecision already validated repairability and consumed one
    // unit from the shared command-policy budget.
    const nextGeneration = controlGeneration
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
          maxSmellVerifyCycles,
          pending: true,
          dispatching: false,
          awaitingVerify: false,
          awaitingVerifyReason: "continuation",
          verifyReminderGeneration: -1,
          agent: input.agent,
          directory: input.directory,
          failureCategory: preparedOutput.failureCategory,
          instruction,
          updatedAt: Date.now(),
        }
    // When mutating in place, update the fields that changed.
    if (hasInflightDispatch) {
      nextState.taskKey = input.taskKey
      nextState.generation = nextGeneration
      nextState.continuation = continuation
      nextState.maxSmellVerifyCycles = maxSmellVerifyCycles
      nextState.pending = true
      nextState.awaitingVerify = false
      nextState.awaitingVerifyReason = "continuation"
      nextState.verifyReminderGeneration = -1
      nextState.agent = input.agent
      nextState.directory = input.directory
      nextState.failureCategory = preparedOutput.failureCategory
      nextState.instruction = instruction
      nextState.updatedAt = Date.now()
    }
    states.set(input.sessionID, nextState)
    log("smell-idle-continue armed", {
      sessionID: input.sessionID,
      generation: nextState.generation,
      taskKey: nextState.taskKey,
      category: nextState.failureCategory,
      continuation: nextState.continuation,
      maxSmellVerifyCycles: nextState.maxSmellVerifyCycles,
    })
    return {
      ...base,
      continuation: nextState.continuation,
      maxSmellVerifyCycles: nextState.maxSmellVerifyCycles,
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

    if (state.continuation <= 0 || state.continuation > state.maxSmellVerifyCycles) return false
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
          maxSmellVerifyCycles: state.maxSmellVerifyCycles,
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
    rehydrateFromControl,
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
  const refactoringBackend = payload?.refactoring_backend
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
  if (refactoringBackend !== "direct" && refactoringBackend !== "idea") {
    throw new Error("INVALID_LOOP_POLICY: resolver returned an unsupported refactoring backend")
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
  if (refactoringBackend === "idea" && String(identity.language || "").toLowerCase() !== "java") {
    throw new Error("IDEA_BACKEND_REQUIRES_JAVA: IDEA backend requires an explicit Java identity")
  }
  if (typeof payload.checkpoint_required !== "boolean") {
    throw new Error("INVALID_COMMAND_TASK_IDENTITY: resolver returned no checkpoint requirement")
  }
  if (!loop || !["off", "verify-failure"].includes(String(loop.mode || ""))) {
    throw new Error("INVALID_LOOP_POLICY: resolver returned an invalid loop mode")
  }
  if (!Number.isInteger(loop.max_smell_verify_cycles) || Number(loop.max_smell_verify_cycles) < 0 || Number(loop.max_smell_verify_cycles) > 10) {
    throw new Error("INVALID_LOOP_POLICY: resolver returned an invalid smell-verify cycle limit")
  }
  if (!Number.isInteger(loop.no_progress_limit) || Number(loop.no_progress_limit) < 1 || Number(loop.no_progress_limit) > 5) {
    throw new Error("INVALID_LOOP_POLICY: resolver returned an invalid no-progress limit")
  }
  if (
    !Array.isArray(loop.allowed_failure_groups)
    || !loop.allowed_failure_groups.every((item) => typeof item === "string" && allowedFailureGroups.has(item))
    || (loop.mode !== "off" && Number(loop.max_smell_verify_cycles) > 0 && loop.allowed_failure_groups.length === 0)
  ) {
    throw new Error("INVALID_LOOP_POLICY: resolver returned invalid failure groups")
  }
  if (typeof loop.instruction !== "string" || !loop.instruction.trim()) {
    throw new Error("INVALID_LOOP_POLICY: resolver returned no smell-verify repair instruction")
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
    refactoring_backend: refactoringBackend,
    allow_test_changes: payload.allow_test_changes,
    checkpoint_required: payload.checkpoint_required,
    identity: Object.fromEntries(
      COMMAND_IDENTITY_FIELDS.map(([key]) => [key, identity[key]]),
    ) as CommandTaskIdentity,
    loop: {
      mode: loop.mode as LoopPolicy["mode"],
      max_smell_verify_cycles: Number(loop.max_smell_verify_cycles),
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
    lines.push("- After one coherent production edit, call smell_verify. While the source Guard is still above the passing route, it performs only the source check and cannot accept the sample or execute project_full.")
    lines.push("- Follow source_guard_feedback for the next narrow correction. Do not manually run a heavy project build in the candidate source tree.")
    lines.push("- Once the source Guard crosses a passing route, the same smell_verify call advances to final acceptance under the controller-owned verification mode.")
  }
  lines.push("- The frozen target Guard and build/test result are the acceptance authority; do not scan or rewrite unrelated sources.")
  lines.push("- Read mutable remaining counts, worklists, and next actions only from the latest smell_verify tool result.")
  return lines.join("\n")
}

function commandControllerSystemContext(
  policy: CommandPolicy,
  targetIdentityContext: string = "",
): string {
  const allowed = policy.loop.allowed_failure_groups.join(", ") || "none"
  const backend = policy.refactoring_backend
  const lines = [
    '<smell-controller-context schema="1">',
    "This stable controller context supplements the original user message; it does not replace it.",
    "Controller-owned verification, identity, and loop policy:",
    "- target_identity: frozen from the original user message and enforced by the controller.",
    `- verification_mode: ${policy.verification_mode}`,
    `- allow_test_changes: ${policy.allow_test_changes}`,
    `- refactoring_backend: ${backend}`,
    `- loop_mode: ${policy.loop.mode}`,
    `- max_smell_verify_cycles: ${policy.loop.max_smell_verify_cycles}`,
    `- no_progress_limit: ${policy.loop.no_progress_limit}`,
    `- allowed_failure_groups: ${allowed}`,
    `- sample_deadline_seconds: ${policy.loop.sample_deadline_seconds}`,
    "",
    policy.checkpoint_required
      ? `smell_verify is the controller-owned staged gate under verification_mode=${policy.verification_mode}: source Guard, then final acceptance only after the Guard passes.`
      : "Call smell_verify as the acceptance gate. Its loop.decision field is authoritative.",
    "Whenever smell_verify returns loop.decision=continue, read loop.instruction from that tool result before one narrow correction.",
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
      "- Call smell_verify for every compile or test; it owns final project_full verification after the source Guard passes.",
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

function newCommandLoopState(
  policy: CommandPolicy,
  targetIdentityContext: string = "",
  startedAt: number = Date.now(),
): CommandLoopState {
  return {
    policy,
    targetIdentityContext,
    startedAt,
    control: {
      generation: 0,
      decision: "verify_required",
      instruction: INITIAL_VERIFY_INSTRUCTION,
      terminationReason: "",
    },
    smellVerifyCycleCount: 0,
    noProgressCount: 0,
    lastFailureFingerprint: "",
    bestMetricDeficit: null,
    bestStructuralFailureCount: null,
    lastBlockerCodes: [],
    seenStructuralStates: [],
    formalCandidateState: {
      candidateIdentity: null,
      outcome: "",
      diagnosticSignature: "",
      confirmationRequired: false,
    },
    ideaProtocolState: newIdeaProtocolState(),
    terminalReceipt: null,
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
    control: {
      generation: state.control.generation,
      decision: state.control.decision,
      instruction: state.control.instruction,
      termination_reason: state.control.terminationReason,
    },
    smell_verify_cycle_count: state.smellVerifyCycleCount,
    no_progress_count: state.noProgressCount,
    last_failure_fingerprint: state.lastFailureFingerprint,
    best_metric_deficit: state.bestMetricDeficit ?? null,
    best_structural_failure_count: state.bestStructuralFailureCount ?? null,
    last_blocker_codes: state.lastBlockerCodes,
    seen_structural_states: state.seenStructuralStates,
    formal_candidate_state: {
      candidate_identity: state.formalCandidateState.candidateIdentity
        ? {
            baseline_revision: state.formalCandidateState.candidateIdentity.baselineRevision,
            baseline_tree: state.formalCandidateState.candidateIdentity.baselineTree,
            production_diff: state.formalCandidateState.candidateIdentity.productionDiff,
            test_tree: state.formalCandidateState.candidateIdentity.testTree,
            verification_config_tree: state.formalCandidateState.candidateIdentity.verificationConfigTree,
          }
        : null,
      outcome: state.formalCandidateState.outcome,
      diagnostic_signature: state.formalCandidateState.diagnosticSignature,
      confirmation_required: state.formalCandidateState.confirmationRequired,
    },
    idea_protocol_state: ideaProtocolStateSnapshot(state.ideaProtocolState),
    terminal_receipt: state.terminalReceipt ?? null,
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
    const smellVerifyCycleCount = Number(parsed.smell_verify_cycle_count)
    const noProgressCount = Number(parsed.no_progress_count)
    const bestMetricDeficit = parsed.best_metric_deficit === null
      ? null
      : Number(parsed.best_metric_deficit)
    const bestStructuralFailureCount = parsed.best_structural_failure_count === null
      ? null
      : Number(parsed.best_structural_failure_count)
    const lastBlockerCodes = asStringArray(parsed.last_blocker_codes)
    const seenStructuralStates = asStringArray(parsed.seen_structural_states)
    const formalCandidateRecord = recordValue(parsed.formal_candidate_state)
    const rawCandidateIdentity = formalCandidateRecord?.candidate_identity
    const candidateIdentityRecord = rawCandidateIdentity === null
      ? null
      : recordValue(rawCandidateIdentity)
    const formalOutcome = formalCandidateRecord?.outcome
    const formalDiagnosticSignature = formalCandidateRecord?.diagnostic_signature
    const formalConfirmationRequired = formalCandidateRecord?.confirmation_required
    const ideaProtocolState = restoreIdeaProtocolState(parsed.idea_protocol_state)
    const terminalReceipt = parsed.terminal_receipt === null
      ? null
      : recordValue(parsed.terminal_receipt)
    const rawTerminalFormalReceipt = terminalReceipt?.formalVerificationReceipt
    const terminalFormalReceipt = rawTerminalFormalReceipt === null
      ? null
      : recordValue(rawTerminalFormalReceipt)
    const rawTerminalIdeaReceipt = terminalReceipt?.ideaProtocolReceipt
    const terminalIdeaReceipt = rawTerminalIdeaReceipt === null
      ? null
      : recordValue(rawTerminalIdeaReceipt)
    const control = recordValue(parsed.control)
    const terminalLoop = terminalReceipt ? recordValue(terminalReceipt.loop) : null
    const targetIdentityContext = parsed.target_identity_context === undefined
      ? ""
      : parsed.target_identity_context
    if (
      !Number.isFinite(startedAt)
      || !control
      || !Number.isInteger(Number(control.generation))
      || Number(control.generation) < 0
      || !["verify_required", "continue", "stop"].includes(String(control.decision || ""))
      || typeof control.instruction !== "string"
      || typeof control.termination_reason !== "string"
      || (control.decision === "verify_required" && Number(control.generation) !== 0)
      || (control.decision === "verify_required" && control.instruction !== INITIAL_VERIFY_INSTRUCTION)
      || (control.decision === "verify_required" && control.termination_reason !== "")
      || (control.decision === "continue" && (!control.instruction || control.termination_reason !== ""))
      || (control.decision === "stop" && control.instruction !== "")
      || !Number.isInteger(smellVerifyCycleCount)
      || smellVerifyCycleCount < 0
      || smellVerifyCycleCount > loop.max_smell_verify_cycles
      || !Number.isInteger(noProgressCount)
      || noProgressCount < 0
      || typeof parsed.last_failure_fingerprint !== "string"
      || (bestMetricDeficit !== null && (!Number.isFinite(bestMetricDeficit) || bestMetricDeficit < 0))
      || (bestStructuralFailureCount !== null && (!Number.isInteger(bestStructuralFailureCount) || bestStructuralFailureCount < 0))
      || !Array.isArray(parsed.last_blocker_codes)
      || lastBlockerCodes.length !== parsed.last_blocker_codes.length
      || lastBlockerCodes.length > MAX_SEEN_STRUCTURAL_STATES
      || !Array.isArray(parsed.seen_structural_states)
      || seenStructuralStates.length !== parsed.seen_structural_states.length
      || seenStructuralStates.length > MAX_SEEN_STRUCTURAL_STATES
      || new Set(seenStructuralStates).size !== seenStructuralStates.length
      || !formalCandidateRecord
      || (rawCandidateIdentity !== null && !candidateIdentityRecord)
      || !["", "pass", "test_failed", "failed"].includes(String(formalOutcome ?? ""))
      || typeof formalDiagnosticSignature !== "string"
      || formalDiagnosticSignature.length > 128
      || typeof formalConfirmationRequired !== "boolean"
      || !ideaProtocolState
      || (candidateIdentityRecord !== null && (
        typeof candidateIdentityRecord.baseline_revision !== "string"
        || !candidateIdentityRecord.baseline_revision
        || candidateIdentityRecord.baseline_revision.length > 128
        || typeof candidateIdentityRecord.baseline_tree !== "string"
        || candidateIdentityRecord.baseline_tree.length > 128
        || typeof candidateIdentityRecord.production_diff !== "string"
        || !candidateIdentityRecord.production_diff
        || candidateIdentityRecord.production_diff.length > 128
        || typeof candidateIdentityRecord.test_tree !== "string"
        || (
          String(policy.identity.language || "").toLowerCase() === "java"
          && !candidateIdentityRecord.test_tree
        )
        || candidateIdentityRecord.test_tree.length > 128
        || typeof candidateIdentityRecord.verification_config_tree !== "string"
        || (
          String(policy.identity.language || "").toLowerCase() === "java"
          && !candidateIdentityRecord.verification_config_tree
        )
        || candidateIdentityRecord.verification_config_tree.length > 128
        || formalOutcome === ""
        || !formalDiagnosticSignature
      ))
      || (candidateIdentityRecord === null && (
        formalOutcome !== ""
        || formalDiagnosticSignature !== ""
        || formalConfirmationRequired !== false
      ))
      || (terminalReceipt !== null && (
        typeof terminalReceipt.status !== "string"
        || !["cheap_guard", "formal_verify", "protocol"].includes(String(terminalReceipt.stage || ""))
        || typeof terminalReceipt.success !== "boolean"
        || typeof terminalReceipt.accepted !== "boolean"
        || typeof terminalReceipt.resolution !== "string"
        || typeof terminalReceipt.terminationReason !== "string"
        || typeof terminalReceipt.failureCategory !== "string"
        || typeof terminalReceipt.failureGroup !== "string"
        || !("formalVerificationReceipt" in terminalReceipt)
        || !("ideaProtocolReceipt" in terminalReceipt)
        || (terminalReceipt.stage === "formal_verify" && terminalFormalReceipt && (
          terminalFormalReceipt.schema_version !== FORMAL_VERIFICATION_RECEIPT_SCHEMA
          || terminalFormalReceipt.terminal_stage !== "formal_verify"
          || terminalFormalReceipt.status !== terminalReceipt.status
          || terminalFormalReceipt.success !== terminalReceipt.success
          || terminalFormalReceipt.accepted !== terminalReceipt.accepted
          || terminalFormalReceipt.resolution !== terminalReceipt.resolution
        ))
        || (terminalReceipt.stage === "formal_verify" && terminalReceipt.accepted === true && !terminalFormalReceipt)
        || (terminalReceipt.stage !== "formal_verify" && rawTerminalFormalReceipt !== null)
        || (terminalIdeaReceipt !== null && (
          terminalReceipt.stage !== "formal_verify"
          || policy.refactoring_backend !== "idea"
          || !ideaProtocolReceiptMatchesState(terminalIdeaReceipt, ideaProtocolState)
        ))
        || (terminalReceipt.stage === "formal_verify" && policy.refactoring_backend === "idea" && !terminalIdeaReceipt)
        || (terminalReceipt.stage !== "formal_verify" && rawTerminalIdeaReceipt !== null)
        || (terminalReceipt.accepted === true && policy.refactoring_backend === "idea" && terminalIdeaReceipt?.complete !== true)
        || !terminalLoop
        || terminalLoop.decision !== "stop"
        || terminalReceipt.terminationReason !== terminalLoop.termination_reason
        || (terminalReceipt.accepted === true && terminalReceipt.success !== true)
      ))
      || (terminalReceipt === null && control.decision === "stop")
      || (terminalReceipt !== null && (
        control.decision !== "stop"
        || terminalReceipt.terminationReason !== control.termination_reason
        || Number(control.generation) !== Number(terminalLoop?.generation)
      ))
      || typeof targetIdentityContext !== "string"
      || targetIdentityContext.length > 32768
    ) return undefined
    return {
      policy,
      targetIdentityContext,
      startedAt,
      control: {
        generation: Number(control.generation),
        decision: control.decision as CommandControlState["decision"],
        instruction: control.instruction,
        terminationReason: control.termination_reason,
      },
      smellVerifyCycleCount,
      noProgressCount,
      lastFailureFingerprint: parsed.last_failure_fingerprint,
      bestMetricDeficit,
      bestStructuralFailureCount,
      lastBlockerCodes,
      seenStructuralStates,
      formalCandidateState: {
        candidateIdentity: candidateIdentityRecord
          ? {
              baselineRevision: String(candidateIdentityRecord.baseline_revision),
              baselineTree: String(candidateIdentityRecord.baseline_tree),
              productionDiff: String(candidateIdentityRecord.production_diff),
              testTree: String(candidateIdentityRecord.test_tree),
              verificationConfigTree: String(candidateIdentityRecord.verification_config_tree),
            }
          : null,
        outcome: String(formalOutcome) as FormalCandidateState["outcome"],
        diagnosticSignature: formalDiagnosticSignature,
        confirmationRequired: formalConfirmationRequired,
      },
      ideaProtocolState,
      terminalReceipt: terminalReceipt as CommandTerminalReceipt | null,
    }
  } catch {
    return undefined
  }
}

function commandDeadlineEpochMs(state: CommandLoopState): number {
  return state.startedAt + state.policy.loop.sample_deadline_seconds * 1000
}

function commandSessionStateRoot(
  env: Readonly<Record<string, string | undefined>> = process.env,
): string {
  const explicit = String(env[COMMAND_SESSION_STATE_ROOT_ENV] || "").trim()
  if (explicit) return path.resolve(explicit)
  const xdgStateHome = String(env.XDG_STATE_HOME || "").trim()
  const stateHome = xdgStateHome ? path.resolve(xdgStateHome) : path.join(homedir(), ".local", "state")
  return path.join(stateHome, "opencode", "smell-refactor")
}

function commandSessionStateFile(
  sessionID: string,
  env: Readonly<Record<string, string | undefined>> = process.env,
): string {
  if (!sessionID) throw new Error("COMMAND_SESSION_ID_MISSING")
  const encodedSessionID = Buffer.from(sessionID, "utf8").toString("base64url")
  return path.join(commandSessionStateRoot(env), "sessions", `${encodedSessionID}.json`)
}

function commandSessionLineageFile(
  sessionID: string,
  env: Readonly<Record<string, string | undefined>> = process.env,
): string {
  if (!sessionID) throw new Error("COMMAND_SESSION_ID_MISSING")
  const encodedSessionID = Buffer.from(sessionID, "utf8").toString("base64url")
  return path.join(commandSessionStateRoot(env), "lineage", `${encodedSessionID}.json`)
}

let commandSessionStateWriteSequence = 0

function writeCommandSessionState(input: {
  sessionID: string
  worktree: string
  state: CommandLoopState
  baselineSeal: string
  command: CommandSessionMetadata["command"]
  agent: CommandSessionMetadata["agent"]
  initialization: CommandSessionMetadata["initialization"]
  env?: Readonly<Record<string, string | undefined>>
}): void {
  const file = commandSessionStateFile(input.sessionID, input.env)
  const directory = path.dirname(file)
  mkdirSync(directory, { recursive: true, mode: 0o700 })
  chmodSync(commandSessionStateRoot(input.env), 0o700)
  chmodSync(directory, 0o700)
  const payload = {
    schema_version: COMMAND_SESSION_STATE_SCHEMA,
    session_id: input.sessionID,
    worktree: path.resolve(input.worktree),
    identity: input.state.policy.identity,
    deadline_epoch_ms: commandDeadlineEpochMs(input.state),
    baseline_seal: input.baselineSeal,
    command: input.command,
    agent: input.agent,
    initialization: input.initialization,
    command_loop_state: commandLoopStateSnapshot(input.state),
  }
  commandSessionStateWriteSequence += 1
  const temporary = `${file}.${process.pid}.${commandSessionStateWriteSequence}.tmp`
  try {
    writeFileSync(temporary, `${safeJsonStringify(payload)}\n`, {
      encoding: "utf8",
      flag: "wx",
      mode: 0o600,
    })
    renameSync(temporary, file)
    chmodSync(file, 0o600)
  } finally {
    if (existsSync(temporary)) unlinkSync(temporary)
  }
}

function readCommandSessionState(input: {
  sessionID: string
  worktree: string
  env?: Readonly<Record<string, string | undefined>>
}): {
  state: CommandLoopState
  baselineSeal: string
  metadata: CommandSessionMetadata
} | undefined {
  const file = commandSessionStateFile(input.sessionID, input.env)
  if (!existsSync(file)) return undefined
  let parsed: Record<string, unknown> | null
  try {
    parsed = recordValue(JSON.parse(readFileSync(file, "utf8")))
  } catch {
    throw new Error("COMMAND_SESSION_STATE_INVALID: persisted state is not valid JSON")
  }
  const stateRecord = recordValue(parsed?.command_loop_state)
  const state = restoreCommandLoopState(stateRecord ? safeJsonStringify(stateRecord) : undefined)
  const baselineSeal = parsed?.baseline_seal
  const command = parsed?.command
  const agent = parsed?.agent
  const initialization = parsed?.initialization
  const storedIdentity = recordValue(parsed?.identity)
  if (
    !parsed
    || parsed.schema_version !== COMMAND_SESSION_STATE_SCHEMA
    || parsed.session_id !== input.sessionID
    || parsed.worktree !== path.resolve(input.worktree)
    || !state
    || !storedIdentity
    || safeJsonStringify(storedIdentity) !== safeJsonStringify(state.policy.identity)
    || Number(parsed.deadline_epoch_ms) !== commandDeadlineEpochMs(state)
    || typeof baselineSeal !== "string"
    || !["smell-refactor-run", "java-refactor-run"].includes(String(command || ""))
    || !["smell-refactor-agent", "java-refactor-agent"].includes(String(agent || ""))
    || !["baseline_pending", "ready"].includes(String(initialization || ""))
    || (command === "smell-refactor-run" && agent !== "smell-refactor-agent")
    || (command === "java-refactor-run" && agent !== "java-refactor-agent")
    || (initialization === "ready" && state.policy.checkpoint_required && !baselineSeal)
  ) {
    throw new Error("COMMAND_SESSION_STATE_INVALID: persisted command identity, worktree, or deadline does not match")
  }
  return {
    state,
    baselineSeal,
    metadata: {
      command: command as CommandSessionMetadata["command"],
      agent: agent as CommandSessionMetadata["agent"],
      initialization: initialization as CommandSessionMetadata["initialization"],
    },
  }
}

function deleteCommandSessionState(
  sessionID: string,
  env: Readonly<Record<string, string | undefined>> = process.env,
): void {
  const file = commandSessionStateFile(sessionID, env)
  try {
    unlinkSync(file)
  } catch (error) {
    if ((error as NodeJS.ErrnoException)?.code !== "ENOENT") throw error
  }
}

function writeCommandSessionLineage(
  sessionID: string,
  parentID: string,
  env: Readonly<Record<string, string | undefined>> = process.env,
): void {
  if (!sessionID || !parentID || sessionID === parentID) {
    throw new Error("COMMAND_SESSION_LINEAGE_INVALID")
  }
  const file = commandSessionLineageFile(sessionID, env)
  const directory = path.dirname(file)
  mkdirSync(directory, { recursive: true, mode: 0o700 })
  chmodSync(commandSessionStateRoot(env), 0o700)
  chmodSync(directory, 0o700)
  commandSessionStateWriteSequence += 1
  const temporary = `${file}.${process.pid}.${commandSessionStateWriteSequence}.tmp`
  const payload = {
    schema_version: COMMAND_SESSION_LINEAGE_SCHEMA,
    session_id: sessionID,
    parent_id: parentID,
  }
  try {
    writeFileSync(temporary, `${safeJsonStringify(payload)}\n`, {
      encoding: "utf8",
      flag: "wx",
      mode: 0o600,
    })
    renameSync(temporary, file)
    chmodSync(file, 0o600)
  } finally {
    if (existsSync(temporary)) unlinkSync(temporary)
  }
}

function readCommandSessionParent(
  sessionID: string,
  env: Readonly<Record<string, string | undefined>> = process.env,
): string | undefined {
  const file = commandSessionLineageFile(sessionID, env)
  if (!existsSync(file)) return undefined
  let parsed: Record<string, unknown> | null
  try {
    parsed = recordValue(JSON.parse(readFileSync(file, "utf8")))
  } catch {
    throw new Error("COMMAND_SESSION_LINEAGE_INVALID: persisted lineage is not valid JSON")
  }
  if (
    !parsed
    || parsed.schema_version !== COMMAND_SESSION_LINEAGE_SCHEMA
    || parsed.session_id !== sessionID
    || typeof parsed.parent_id !== "string"
    || !parsed.parent_id
    || parsed.parent_id === sessionID
  ) {
    throw new Error("COMMAND_SESSION_LINEAGE_INVALID: persisted lineage does not match the session")
  }
  return parsed.parent_id
}

function deleteCommandSessionLineage(
  sessionID: string,
  env: Readonly<Record<string, string | undefined>> = process.env,
): void {
  const file = commandSessionLineageFile(sessionID, env)
  try {
    unlinkSync(file)
  } catch (error) {
    if ((error as NodeJS.ErrnoException)?.code !== "ENOENT") throw error
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
    generation: state.control.generation + 1,
    decision: input.decision,
    termination_reason: input.terminationReason,
    continuation: state.smellVerifyCycleCount,
    max_smell_verify_cycles: state.policy.loop.max_smell_verify_cycles,
    remaining: Math.max(0, state.policy.loop.max_smell_verify_cycles - state.smellVerifyCycleCount),
    no_progress_count: state.noProgressCount,
    no_progress_limit: state.policy.loop.no_progress_limit,
    elapsed_seconds: input.elapsedSeconds,
    sample_deadline_seconds: state.policy.loop.sample_deadline_seconds,
    failure_category: input.failureCategory,
    failure_group: input.failureGroup,
    instruction: input.instruction,
  }
}

function advanceCommandControl(
  state: CommandLoopState,
  loop: Record<string, unknown>,
): void {
  const decision = loop.decision
  const generation = Number(loop.generation)
  const instruction = loop.instruction
  const terminationReason = loop.termination_reason
  if (
    (decision !== "continue" && decision !== "stop")
    || !Number.isInteger(generation)
    || generation !== state.control.generation + 1
    || typeof instruction !== "string"
    || typeof terminationReason !== "string"
    || (decision === "continue" && (!instruction || terminationReason))
    || (decision === "stop" && instruction)
  ) {
    throw new Error("COMMAND_CONTROL_TRANSITION_INVALID")
  }
  state.control = {
    generation,
    decision,
    instruction,
    terminationReason,
  }
}

function latchCommandTerminal(
  state: CommandLoopState,
  payload: Record<string, unknown>,
  loop: Record<string, unknown>,
  stage: CommandTerminalReceipt["stage"],
): void {
  if (state.terminalReceipt) return
  if (loop.decision !== "stop") throw new Error("COMMAND_LOOP_TERMINAL_DECISION_INVALID")
  const ideaReceipt = stage === "formal_verify" && state.policy.refactoring_backend === "idea"
    ? ideaProtocolReceipt(state.ideaProtocolState)
    : null
  if (payload.accepted === true && ideaReceipt && ideaReceipt.complete !== true) {
    throw new Error("IDEA_PROTOCOL_INCOMPLETE: accepted formal receipt is not bound to an IDEA mutation")
  }
  if (ideaReceipt) payload.idea_protocol_receipt = ideaReceipt
  state.terminalReceipt = {
    stage,
    status: typeof payload.status === "string" ? payload.status : "TERMINAL",
    success: payload.success === true,
    accepted: payload.accepted === true,
    resolution: typeof payload.resolution === "string" ? payload.resolution : "",
    terminationReason: typeof loop.termination_reason === "string"
      ? loop.termination_reason
      : "TERMINAL",
    failureCategory: typeof loop.failure_category === "string" ? loop.failure_category : "",
    failureGroup: typeof loop.failure_group === "string" ? loop.failure_group : "",
    formalVerificationReceipt: stage === "formal_verify"
      ? recordValue(payload.formal_verification_receipt)
      : null,
    ideaProtocolReceipt: ideaReceipt,
    loop: toJsonSafe(loop) as Record<string, unknown>,
  }
}

function applyProtocolTerminalDecision(
  normalized: { output: string; metadata: Record<string, unknown> },
  state: CommandLoopState,
  input: { status: string; failureCategory: string; message: string },
): PreparedLoopOutput {
  const elapsedSeconds = Math.max(0, Math.floor((Date.now() - state.startedAt) / 1000))
  const payload: Record<string, unknown> = {
    schema_version: "smell.plugin-protocol-terminal/v1",
    success: false,
    accepted: false,
    status: input.status,
    resolution: "rejected",
    message: input.message,
  }
  const loop = buildLoopDecision(state, {
    decision: "stop",
    terminationReason: input.status,
    elapsedSeconds,
    failureCategory: input.failureCategory,
    failureGroup: "controller",
    instruction: "",
  })
  advanceCommandControl(state, loop)
  payload.loop = loop
  latchCommandTerminal(state, payload, loop, "protocol")
  normalized.output = safeJsonStringify(payload)
  normalized.metadata.loop = toJsonSafe(loop)
  return { payload, failureCategory: input.failureCategory }
}

function renderCommandTerminalReceipt(
  name: string,
  state: CommandLoopState,
): { output: string; metadata: Record<string, unknown> } {
  const receipt = state.terminalReceipt
  if (!receipt) throw new Error("COMMAND_LOOP_TERMINAL_RECEIPT_MISSING")
  const payload = {
    schema_version: "smell.loop-terminal/v1",
    terminal: true,
    stage: receipt.stage,
    success: receipt.success,
    accepted: receipt.accepted,
    status: receipt.status,
    resolution: receipt.resolution,
    termination_reason: receipt.terminationReason,
    failure_category: receipt.failureCategory,
    failure_group: receipt.failureGroup,
    formal_verification_receipt: receipt.formalVerificationReceipt,
    idea_protocol_receipt: receipt.ideaProtocolReceipt,
    message: "This command is terminal. Start a new smell-refactor command for a new attempt.",
    loop: receipt.loop,
  }
  const normalized = normalizeToolResult(name, {
    exitCode: 0,
    stdout: safeJsonStringify(payload),
    stderr: "",
    json: payload,
  })
  normalized.metadata.loop = toJsonSafe(receipt.loop)
  normalized.metadata.command_loop_state = toJsonSafe(commandLoopStateSnapshot(state))
  return normalized
}

function nonNegativeFinite(value: unknown): number | null {
  const number = Number(value)
  return Number.isFinite(number) && number >= 0 ? number : null
}

function nonNegativeInteger(value: unknown): number | null {
  const number = Number(value)
  return Number.isInteger(number) && number >= 0 ? number : null
}

function guardProgressObservation(payload: Record<string, unknown>): {
  metricDeficit: number
  structuralFailureCount: number
  blockerCodes: string[]
} {
  const feedback = recordValue(payload.source_guard_feedback)
  const observation = recordValue(feedback?.progress_observation)
  let metricDeficit = nonNegativeFinite(observation?.metric_deficit)
  if (metricDeficit === null) {
    const rawBudget = feedback?.metric_budget ?? payload.metric_budget
    const budgets = Array.isArray(rawBudget) ? rawBudget : rawBudget ? [rawBudget] : []
    metricDeficit = budgets.reduce((total, item) => {
      const budget = recordValue(item)
      return total + (nonNegativeFinite(budget?.required_reduction) ?? 0)
    }, 0)
  }
  let structuralFailureCount = nonNegativeInteger(
    observation?.structural_failure_count
      ?? payload.guard_failure_count,
  )
  if (structuralFailureCount === null) {
    const blocker = recordValue(feedback?.blocker)
    const blockerKind = typeof blocker?.kind === "string" ? blocker.kind.trim() : ""
    structuralFailureCount = blockerKind && blockerKind !== "metric_budget" ? 1 : 0
  }
  const blocker = recordValue(feedback?.blocker)
  const blockerCodes = Array.from(new Set([
    ...asStringArray(observation?.blocker_codes),
    typeof blocker?.code === "string" ? blocker.code : "",
  ].map((code) => code.trim()).filter(Boolean))).sort().slice(0, MAX_SEEN_STRUCTURAL_STATES)
  return { metricDeficit, structuralFailureCount, blockerCodes }
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

function formalVerificationObservation(
  payload: Record<string, unknown>,
  requireJavaEvidence: boolean = false,
): {
  receipt: Record<string, unknown>
  candidateIdentity: FormalCandidateIdentity
  outcome: Exclude<FormalCandidateState["outcome"], "">
  diagnosticSignature: string
} | null {
  const receipt = recordValue(payload.formal_verification_receipt)
  const identity = recordValue(receipt?.candidate_identity)
  const guard = recordValue(receipt?.guard)
  const buildTest = recordValue(receipt?.build_test)
  const artifactRefs = recordValue(receipt?.artifact_refs)
  const freshIsolation = receipt?.fresh_isolation
  const outcome = receipt?.outcome
  const diagnosticSignature = typeof receipt?.diagnostic_signature === "string"
    ? receipt.diagnostic_signature.trim()
    : ""
  const baselineRevision = typeof identity?.baseline_revision === "string"
    ? identity.baseline_revision.trim()
    : ""
  const baselineTree = typeof identity?.baseline_tree === "string"
    ? identity.baseline_tree.trim()
    : ""
  const productionDiff = typeof identity?.production_diff === "string"
    ? identity.production_diff.trim()
    : ""
  const testTree = typeof identity?.test_tree === "string"
    ? identity.test_tree.trim()
    : ""
  const verificationConfigTree = typeof identity?.verification_config_tree === "string"
    ? identity.verification_config_tree.trim()
    : ""
  if (
    !receipt
    || receipt.schema_version !== FORMAL_VERIFICATION_RECEIPT_SCHEMA
    || receipt.terminal_stage !== "formal_verify"
    || receipt.status !== payload.status
    || receipt.success !== payload.success
    || receipt.accepted !== payload.accepted
    || receipt.resolution !== payload.resolution
    || !guard
    || !buildTest
    || !artifactRefs
    || (freshIsolation !== null && !recordValue(freshIsolation))
    || !baselineRevision
    || baselineRevision.length > 128
    || baselineTree.length > 128
    || !productionDiff
    || productionDiff.length > 128
    || (requireJavaEvidence && !testTree)
    || testTree.length > 128
    || (requireJavaEvidence && !verificationConfigTree)
    || verificationConfigTree.length > 128
    || !["pass", "test_failed", "failed"].includes(String(outcome || ""))
    || !diagnosticSignature
    || diagnosticSignature.length > 128
  ) return null
  return {
    receipt,
    candidateIdentity: {
      baselineRevision,
      baselineTree,
      productionDiff,
      testTree,
      verificationConfigTree,
    },
    outcome: outcome as Exclude<FormalCandidateState["outcome"], "">,
    diagnosticSignature,
  }
}

function sameFormalCandidate(
  left: FormalCandidateIdentity | null,
  right: FormalCandidateIdentity,
): boolean {
  return Boolean(
    left
    && left.baselineRevision === right.baselineRevision
    && left.baselineTree === right.baselineTree
    && left.productionDiff === right.productionDiff
    && left.testTree === right.testTree
    && left.verificationConfigTree === right.verificationConfigTree
  )
}

function rejectInvalidFormalReceipt(payload: Record<string, unknown>): void {
  payload.success = false
  payload.accepted = false
  payload.progress = false
  payload.status = "FORMAL_VERIFICATION_RECEIPT_INVALID"
  payload.resolution = "rejected"
  payload.continue_hint = ""
  payload.failure_pack = {
    failure_category: "FORMAL_VERIFICATION_RECEIPT_INVALID",
    failure_group: "controller",
    retryable: false,
    verify_status: "FORMAL_VERIFICATION_RECEIPT_INVALID",
    highlights: ["The formal verification result did not include one valid product receipt."],
    next_action: "",
    artifact_paths: {},
    recommendations: [],
  }
}

function rejectIncompleteIdeaProtocol(payload: Record<string, unknown>): void {
  payload.success = false
  payload.accepted = false
  payload.progress = false
  payload.status = "IDEA_PROTOCOL_INCOMPLETE"
  payload.resolution = "rejected"
  payload.continue_hint = ""
  payload.formal_verification_receipt = null
  payload.failure_pack = {
    failure_category: "IDEA_PROTOCOL_INCOMPLETE",
    failure_group: "controller",
    retryable: false,
    verify_status: "IDEA_PROTOCOL_INCOMPLETE",
    highlights: ["Formal acceptance was not bound to a command-owned IDEA mutation."],
    next_action: "",
    artifact_paths: {},
    recommendations: [],
  }
}

function markFreshConfirmationRequired(
  payload: Record<string, unknown>,
  observation: NonNullable<ReturnType<typeof formalVerificationObservation>>,
  prior: FormalCandidateState,
): void {
  payload.success = false
  payload.accepted = false
  payload.progress = false
  payload.status = "FLAKY_TEST_INCONCLUSIVE"
  payload.resolution = "unresolved"
  payload.continue_hint = FRESH_CONFIRMATION_INSTRUCTION
  payload.failure_fingerprint = [
    "FLAKY_TEST_INCONCLUSIVE",
    observation.candidateIdentity.productionDiff,
    observation.outcome,
    observation.diagnosticSignature,
  ].join(":")
  payload.failure_pack = {
    failure_category: "FLAKY_TEST_INCONCLUSIVE",
    failure_group: "test",
    retryable: true,
    verify_status: "FLAKY_TEST_INCONCLUSIVE",
    highlights: [
      `The same candidate changed formal outcome or diagnostics (${prior.outcome || "none"} -> ${observation.outcome}).`,
    ],
    next_action: FRESH_CONFIRMATION_INSTRUCTION,
    artifact_paths: observation.receipt.artifact_refs,
    recommendations: [FRESH_CONFIRMATION_INSTRUCTION],
  }
  payload.formal_verification_receipt = {
    ...observation.receipt,
    status: "FLAKY_TEST_INCONCLUSIVE",
    success: false,
    accepted: false,
    resolution: "unresolved",
    outcome: "failed",
    consistency: {
      status: "fresh_confirmation_required",
      prior_outcome: prior.outcome,
      prior_diagnostic_signature: prior.diagnosticSignature,
      observed_outcome: observation.outcome,
      observed_diagnostic_signature: observation.diagnosticSignature,
    },
  }
}

function applyFormalVerificationConsistency(
  payload: Record<string, unknown>,
  state: CommandLoopState,
): void {
  const observation = formalVerificationObservation(
    payload,
    String(state.policy.identity.language || "").toLowerCase() === "java",
  )
  if (!observation) {
    if (
      state.policy.checkpoint_required
      && (
        Object.prototype.hasOwnProperty.call(payload, "formal_verification_receipt")
        || (
          payload.status === "PASS"
          && payload.success === true
          && payload.accepted === true
        )
      )
    ) rejectInvalidFormalReceipt(payload)
    return
  }
  if (
    state.policy.verification_mode === "project_full"
    && payload.status === "PASS"
    && payload.success === true
    && payload.accepted === true
    && (
      payload.project_full_executed !== true
      || recordValue(observation.receipt.build_test)?.project_full_executed !== true
    )
  ) {
    rejectInvalidFormalReceipt(payload)
    return
  }
  const prior = state.formalCandidateState
  const sameCandidate = sameFormalCandidate(
    prior.candidateIdentity,
    observation.candidateIdentity,
  )
  const sameObservation = Boolean(
    sameCandidate
    && prior.outcome === observation.outcome
    && prior.diagnosticSignature === observation.diagnosticSignature
  )
  if (sameCandidate && !sameObservation) {
    markFreshConfirmationRequired(payload, observation, prior)
    state.formalCandidateState = {
      candidateIdentity: observation.candidateIdentity,
      outcome: observation.outcome,
      diagnosticSignature: observation.diagnosticSignature,
      confirmationRequired: true,
    }
    return
  }
  state.formalCandidateState = {
    candidateIdentity: observation.candidateIdentity,
    outcome: observation.outcome,
    diagnosticSignature: observation.diagnosticSignature,
    confirmationRequired: false,
  }
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
  applyFormalVerificationConsistency(payload, state)
  if (state.policy.refactoring_backend === "idea") {
    recordIdeaVerifyOutcome(state.ideaProtocolState)
    if (
      payload.status === "PASS"
      && payload.success === true
      && payload.accepted === true
      && ideaProtocolReceipt(state.ideaProtocolState).complete !== true
    ) {
      rejectIncompleteIdeaProtocol(payload)
    }
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
  const freshConfirmationRequired = payload.status === "FLAKY_TEST_INCONCLUSIVE"
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

    if (state.policy.loop.mode === "off" || state.policy.loop.max_smell_verify_cycles <= 0) {
      terminationReason = improvedOnly ? "IMPROVED_LOOP_DISABLED" : "LOOP_DISABLED"
    } else if (!improvedOnly && !retryable) {
      terminationReason = "NON_REPAIRABLE_FAILURE"
    } else if (elapsedSeconds >= state.policy.loop.sample_deadline_seconds) {
      terminationReason = improvedOnly ? "IMPROVED_SAMPLE_DEADLINE" : "SAMPLE_DEADLINE_REACHED"
    } else if (state.smellVerifyCycleCount >= state.policy.loop.max_smell_verify_cycles) {
      terminationReason = freshConfirmationRequired
        ? "FLAKY_TEST_INCONCLUSIVE"
        : improvedOnly ? "IMPROVED_MAX_SMELL_VERIFY_CYCLES" : "MAX_SMELL_VERIFY_CYCLES_REACHED"
    } else {
      state.smellVerifyCycleCount += 1
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
  advanceCommandControl(state, loop)
  if (decision === "stop") {
    latchCommandTerminal(
      state,
      payload,
      loop,
      payload.status === "FORMAL_VERIFICATION_RECEIPT_INVALID"
        || payload.status === "IDEA_PROTOCOL_INCOMPLETE"
        ? "protocol"
        : "formal_verify",
    )
  }
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
): PreparedLoopOutput | null {
  let payload: Record<string, unknown>
  try {
    payload = JSON.parse(normalized.output) as Record<string, unknown>
  } catch {
    return null
  }
  if (state.policy.refactoring_backend === "idea") {
    recordIdeaVerifyOutcome(state.ideaProtocolState)
  }
  const observation = guardProgressObservation(payload)
  const hasBest = state.bestMetricDeficit !== null && state.bestStructuralFailureCount !== null
  const priorMetricDeficit = state.bestMetricDeficit
  const priorStructuralFailureCount = state.bestStructuralFailureCount
  const structuralState = observation.structuralFailureCount > 0
    ? `${observation.structuralFailureCount}:${observation.blockerCodes.join("|") || "unclassified"}`
    : ""
  const unseenStructuralState = Boolean(
    structuralState && !state.seenStructuralStates.includes(structuralState),
  )
  const strictlyImproved = !hasBest
    || (
      priorStructuralFailureCount! > 0
      && (
        observation.structuralFailureCount === 0
        || observation.structuralFailureCount < priorStructuralFailureCount!
        || (
          observation.structuralFailureCount === priorStructuralFailureCount
          && unseenStructuralState
        )
      )
    )
    || (
      priorStructuralFailureCount === 0
      && observation.structuralFailureCount === 0
      && observation.metricDeficit < priorMetricDeficit!
    )
  if (
    structuralState
    && unseenStructuralState
    && state.seenStructuralStates.length < MAX_SEEN_STRUCTURAL_STATES
  ) {
    state.seenStructuralStates.push(structuralState)
  }
  if (strictlyImproved) {
    if (!hasBest || observation.structuralFailureCount < priorStructuralFailureCount!) {
      state.bestStructuralFailureCount = observation.structuralFailureCount
    }
    if (!hasBest || observation.structuralFailureCount === 0) {
      state.bestMetricDeficit = observation.metricDeficit
    }
    state.noProgressCount = 0
  } else {
    state.noProgressCount += 1
  }
  state.lastBlockerCodes = observation.blockerCodes
  state.lastFailureFingerprint = [
    "guard-progress",
    observation.metricDeficit,
    observation.structuralFailureCount,
    observation.blockerCodes.join("|"),
  ].join(":")
  const elapsedSeconds = Math.max(0, Math.floor((Date.now() - state.startedAt) / 1000))
  let decision: "continue" | "stop" = "continue"
  let terminationReason = ""
  if (state.policy.loop.mode === "off" || state.policy.loop.max_smell_verify_cycles <= 0) {
    decision = "stop"
    terminationReason = "LOOP_DISABLED"
  } else if (elapsedSeconds >= state.policy.loop.sample_deadline_seconds) {
    decision = "stop"
    terminationReason = "SAMPLE_DEADLINE_REACHED"
  } else if (state.smellVerifyCycleCount >= state.policy.loop.max_smell_verify_cycles) {
    decision = "stop"
    terminationReason = "MAX_SMELL_VERIFY_CYCLES_REACHED"
  } else {
    state.smellVerifyCycleCount += 1
  }
  const feedback = recordValue(payload.source_guard_feedback)
  const nextAction = typeof feedback?.next_action === "string" && feedback.next_action.trim()
    ? feedback.next_action.trim()
    : typeof payload.next_action === "string"
    ? payload.next_action.trim()
    : ""
  const loop = buildLoopDecision(state, {
    decision,
    terminationReason,
    elapsedSeconds,
    failureCategory: "GUARD_PROGRESS_REQUIRED",
    failureGroup: "smell",
    instruction: decision === "continue"
      ? (nextAction || state.policy.loop.instruction)
      : "",
  })
  payload.progress_observation = {
    metric_deficit: observation.metricDeficit,
    structural_failure_count: observation.structuralFailureCount,
    blocker_codes: observation.blockerCodes,
    strictly_improved: strictlyImproved,
  }
  payload.loop = loop
  advanceCommandControl(state, loop)
  if (decision === "stop") latchCommandTerminal(state, payload, loop, "cheap_guard")
  normalized.output = safeJsonStringify(payload)
  normalized.metadata.loop = toJsonSafe(loop)
  return {
    payload,
    failureCategory: "GUARD_PROGRESS_REQUIRED",
  }
}

export const SmellPlugin: Plugin = async ({ worktree, client }) => {
  const idleRuntime = createIdleContinueRuntime({ client, env: process.env })
  const commandLoopStates = new Map<string, CommandLoopState>()
  const commandSessionParents = new Map<string, string>()
  const commandSessionMetadata = new Map<string, CommandSessionMetadata>()
  const protectedShellLineage = new Set<string>()
  const commandBaselineSeals = new Map<string, string>()
  const commandDeadlineTimers = new Map<string, NodeJS.Timeout>()
  const commandDeadlineAbortDispatched = new Set<string>()
  const commandResolutionInProgress = new Set<string>()
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
  const clearCommandDeadlineTimer = (sessionID: string): void => {
    const timer = commandDeadlineTimers.get(sessionID)
    if (timer) clearTimeout(timer)
    commandDeadlineTimers.delete(sessionID)
  }
  const persistCommandState = (sessionID: string, state: CommandLoopState): void => {
    const metadata = commandSessionMetadata.get(sessionID)
    if (!metadata) {
      if (String(process.env.SMELL_BATCH_RUN || "").trim() === "1") return
      throw new Error("COMMAND_SESSION_METADATA_MISSING")
    }
    writeCommandSessionState({
      sessionID,
      worktree,
      state,
      baselineSeal: commandBaselineSeals.get(sessionID) || "",
      ...metadata,
    })
  }
  const dispatchDeadlineAbort = (sessionID: string): void => {
    if (!shouldPluginHandleSessionIdle(process.env) || commandDeadlineAbortDispatched.has(sessionID)) return
    commandDeadlineAbortDispatched.add(sessionID)
    if (!client?.session?.abort) return
    Promise.resolve(client.session.abort({
      path: { id: sessionID },
      query: { directory: worktree },
    })).catch((error) => {
      // eslint-disable-next-line no-console
      console.error("[smell] deadline abort failed:", error instanceof Error ? error.message : String(error))
    })
  }
  const expireCommandAtDeadline = (sessionID: string, state: CommandLoopState): CommandLoopState => {
    clearCommandDeadlineTimer(sessionID)
    if (!state.terminalReceipt) {
      const elapsedSeconds = Math.max(0, Math.floor((Date.now() - state.startedAt) / 1000))
      const payload: Record<string, unknown> = {
        success: false,
        accepted: false,
        status: "SAMPLE_DEADLINE_REACHED",
        resolution: "rejected",
      }
      const loop = buildLoopDecision(state, {
        decision: "stop",
        terminationReason: "SAMPLE_DEADLINE_REACHED",
        elapsedSeconds,
        failureCategory: "SAMPLE_DEADLINE_REACHED",
        failureGroup: "controller",
        instruction: "",
      })
      advanceCommandControl(state, loop)
      payload.loop = loop
      latchCommandTerminal(state, payload, loop, "protocol")
      idleRuntime.clearSession(sessionID)
      persistCommandState(sessionID, state)
    }
    if (state.terminalReceipt?.terminationReason === "SAMPLE_DEADLINE_REACHED") {
      dispatchDeadlineAbort(sessionID)
    }
    return state
  }
  const armCommandDeadline = (sessionID: string, state: CommandLoopState): void => {
    clearCommandDeadlineTimer(sessionID)
    if (state.terminalReceipt || !shouldPluginHandleSessionIdle(process.env)) return
    const remainingMs = commandDeadlineEpochMs(state) - Date.now()
    if (remainingMs <= 0) {
      expireCommandAtDeadline(sessionID, state)
      return
    }
    const timer = setTimeout(() => {
      expireCommandAtDeadline(sessionID, state)
    }, remainingMs)
    timer.unref()
    commandDeadlineTimers.set(sessionID, timer)
  }
  const persistAndArmCommandState = (sessionID: string, state: CommandLoopState): void => {
    persistCommandState(sessionID, state)
    armCommandDeadline(sessionID, state)
  }
  const rehydrateIdleFromControl = (
    sessionID: string,
    state: CommandLoopState,
    metadata: CommandSessionMetadata,
  ): void => {
    if (metadata.initialization !== "ready") {
      idleRuntime.clearSession(sessionID)
      return
    }
    idleRuntime.rehydrateFromControl({
      sessionID,
      agent: metadata.agent,
      directory: worktree,
      taskKey: makeTaskKey(
        state.policy.identity.project_root,
        state.policy.identity.smell,
        state.policy.identity.location,
      ),
      generation: state.control.generation,
      decision: state.control.decision,
      instruction: state.control.instruction,
      continuation: state.smellVerifyCycleCount,
      maxSmellVerifyCycles: state.policy.loop.max_smell_verify_cycles,
    })
  }
  const restoreCommandLineage = (sessionID: string): void => {
    const visited = new Set<string>()
    let current = sessionID
    while (current && !visited.has(current)) {
      visited.add(current)
      const knownParent = commandSessionParents.get(current)
      const parentID = knownParent || readCommandSessionParent(current) || ""
      if (!parentID || visited.has(parentID)) return
      commandSessionParents.set(current, parentID)
      current = parentID
    }
  }
  const restoreCommandState = (sessionID: string): CommandLoopState | undefined => {
    if (!sessionID) return undefined
    if (commandResolutionInProgress.has(sessionID)) return undefined
    const existing = commandLoopStates.get(sessionID)
    if (existing) {
      return Date.now() >= commandDeadlineEpochMs(existing)
        ? expireCommandAtDeadline(sessionID, existing)
        : existing
    }
    const serializedState = process.env[COMMAND_LOOP_STATE_ENV]
    const preferBatchTransport = String(process.env.SMELL_BATCH_RUN || "").trim() === "1"
      && Boolean(serializedState)
    let restored: CommandLoopState | undefined
    let baselineSeal = ""
    let metadata: CommandSessionMetadata | undefined
    if (!preferBatchTransport) {
      const persisted = readCommandSessionState({ sessionID, worktree })
      restored = persisted?.state
      baselineSeal = persisted?.baselineSeal || ""
      metadata = persisted?.metadata
    }
    if (!restored && serializedState) {
      restored = restoreCommandLoopState(serializedState)
      if (!restored) {
        throw new Error(`COMMAND_POLICY_STATE_INVALID: ${COMMAND_LOOP_STATE_ENV} failed schema validation`)
      }
      assertRestoredCommandIdentity(restored.policy)
      baselineSeal = envDefault("SMELL_BASELINE_SEAL") || ""
      if (!restored.targetIdentityContext && restored.policy.checkpoint_required) {
        restored.targetIdentityContext = checkpointTargetIdentityContextFromFile(
          restored.policy.identity.smell,
          envDefault(BASELINE_CONTEXT_FILE_ENV),
        )
      }
    }
    if (!restored) return undefined
    commandLoopStates.set(sessionID, restored)
    if (baselineSeal) commandBaselineSeals.set(sessionID, baselineSeal)
    if (metadata) commandSessionMetadata.set(sessionID, metadata)
    if (isProtectedProjectFullCandidateShellSession(restored)) {
      markProtectedShellLineage(sessionID)
    }
    if (Date.now() >= commandDeadlineEpochMs(restored)) {
      return expireCommandAtDeadline(sessionID, restored)
    }
    if (metadata?.initialization === "baseline_pending" && !restored.terminalReceipt) {
      const normalized = normalizeToolResult("Command initialization", {
        exitCode: 1,
        stdout: "",
        stderr: "The persisted command stopped before its checkpoint baseline was ready.",
        json: null,
      })
      applyProtocolTerminalDecision(normalized, restored, {
        status: "COMMAND_INITIALIZATION_INCOMPLETE",
        failureCategory: "COMMAND_INITIALIZATION_INCOMPLETE",
        message: "The previous process stopped before checkpoint baseline initialization completed.",
      })
      idleRuntime.clearSession(sessionID)
      persistCommandState(sessionID, restored)
      return restored
    }
    if (metadata) rehydrateIdleFromControl(sessionID, restored, metadata)
    armCommandDeadline(sessionID, restored)
    return restored
  }
  const hasProtectedShellAncestor = (sessionID: string): boolean => {
    restoreCommandLineage(sessionID)
    const visited = new Set<string>()
    let current = commandSessionParents.get(sessionID) || ""
    while (current && !visited.has(current)) {
      visited.add(current)
      const state = restoreCommandState(current)
      if (
        protectedShellLineage.has(current)
        || isProtectedProjectFullCandidateShellSession(state)
      ) return true
      current = commandSessionParents.get(current) || ""
    }
    return false
  }
  const commandBoundaryForSession = (
    sessionID: string,
  ): { ownerSessionID: string; state: CommandLoopState } | undefined => {
    if (!sessionID) return undefined
    restoreCommandLineage(sessionID)
    const direct = restoreCommandState(sessionID)
    if (direct) return { ownerSessionID: sessionID, state: direct }
    const visited = new Set<string>()
    let current = commandSessionParents.get(sessionID) || ""
    while (current && !visited.has(current)) {
      visited.add(current)
      const state = restoreCommandState(current)
      if (state) return { ownerSessionID: current, state }
      current = commandSessionParents.get(current) || ""
    }
    return undefined
  }
  const commandBoundaryStateForSession = (sessionID: string): CommandLoopState | undefined =>
    commandBoundaryForSession(sessionID)?.state
  const terminalStateForSession = (sessionID: string): CommandLoopState | undefined => {
    if (!sessionID) return undefined
    const state = commandBoundaryStateForSession(sessionID)
    return state?.terminalReceipt ? state : undefined
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
    projectRoot: tool.schema.string().optional().describe("Optional source root assertion. When present, it must equal the frozen command project root."),
    ideaProjectRoot: tool.schema
      .string()
      .optional()
      .describe("Optional IDEA root assertion. When present, it must equal the frozen command project root."),
  }
  const assertFrozenIdeaPath = (
    frozenProjectRoot: string,
    candidate: unknown,
    code: string,
  ): void => {
    const raw = typeof candidate === "string" ? candidate.trim() : ""
    if (!raw) return
    const normalizedRoot = path.resolve(frozenProjectRoot)
    const normalizedCandidate = path.resolve(normalizedRoot, raw)
    const relative = path.relative(normalizedRoot, normalizedCandidate)
    if (relative === ".." || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative)) {
      throw new Error(`${code}: '${raw}' is outside the frozen IDEA project root`)
    }
  }
  const ideaCommandBoundaryForSession = (
    sessionID: string,
  ): { ownerSessionID: string; state: CommandLoopState; projectRoot: string } => {
    const boundary = commandBoundaryForSession(sessionID)
    if (!boundary) {
      throw new Error("IDEA_COMMAND_POLICY_REQUIRED: IDEA tools require an active smell-refactor command")
    }
    if (boundary.state.policy.refactoring_backend !== "idea") {
      throw new Error("IDEA_BACKEND_NOT_ENABLED: the frozen command policy selected the direct backend")
    }
    if (String(boundary.state.policy.identity.language || "").toLowerCase() !== "java") {
      throw new Error("IDEA_BACKEND_REQUIRES_JAVA: the frozen command identity is not Java")
    }
    return {
      ...boundary,
      projectRoot: path.resolve(boundary.state.policy.identity.project_root),
    }
  }
  const resolveIdeaCommandInput = (
    args: Record<string, unknown>,
    sessionID: string,
  ) => {
    const boundary = ideaCommandBoundaryForSession(sessionID)
    for (const key of ["projectRoot", "ideaProjectRoot"] as const) {
      const raw = typeof args[key] === "string" ? args[key].trim() : ""
      if (raw && path.resolve(raw) !== boundary.projectRoot) {
        throw new Error(`IDEA_PROJECT_ROOT_MISMATCH: ${key} must match the frozen command project root`)
      }
    }
    if (typeof args.ideaRefactorCli === "string" && args.ideaRefactorCli.trim()) {
      throw new Error("IDEA_CLI_OVERRIDE_FORBIDDEN: the model cannot replace the configured IDEA backend executable")
    }
    assertFrozenIdeaPath(boundary.projectRoot, args.file, "IDEA_FILE_OUTSIDE_PROJECT_ROOT")
    const target = recordValue(args.target)
    assertFrozenIdeaPath(boundary.projectRoot, target?.filePath, "IDEA_TARGET_OUTSIDE_PROJECT_ROOT")
    assertFrozenIdeaPath(boundary.projectRoot, target?.directoryPath, "IDEA_TARGET_OUTSIDE_PROJECT_ROOT")
    const resolved = resolveIdeaInput({
      ideaProjectRoot: boundary.projectRoot,
      language: "java",
    })
    if (!resolved.ok) throw new Error("IDEA_COMMAND_INPUT_INVALID: frozen IDEA command input could not be resolved")
    return { ...boundary, resolved }
  }
  const persistIdeaCommandState = (
    boundary: { ownerSessionID: string; state: CommandLoopState },
  ): void => {
    persistAndArmCommandState(boundary.ownerSessionID, boundary.state)
  }
  const ideaDeadlineForSession = (sessionID: string): number | undefined => {
    if (!sessionID) return undefined
    const state = commandBoundaryStateForSession(sessionID)
    if (!state) return undefined
    if (state.terminalReceipt) {
      throw new Error(
        `SMELL_LOOP_TERMINAL: ${state.terminalReceipt.terminationReason}. `
        + "This command is frozen; start a new smell-refactor-run command for a new attempt.",
      )
    }
    return commandDeadlineEpochMs(state)
  }
  const verifyTool = (name: string) =>
    tool({
      description: "Run the plugin-owned staged Guard: source feedback first, then final configured build/test only after the source Guard passes.",
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
        let commandState = sessionID ? restoreCommandState(sessionID) : undefined
        if (!commandState && sessionID) {
          throw new Error(
            "COMMAND_POLICY_STATE_MISSING: smell_verify requires command-owned state or "
            + `${COMMAND_LOOP_STATE_ENV} from the controller`,
          )
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
        if (commandState && Date.now() >= commandDeadlineEpochMs(commandState)) {
          commandState = expireCommandAtDeadline(sessionID, commandState)
        }
        if (commandState?.terminalReceipt) {
          if (commandState.terminalReceipt.accepted && javaCheckpoint && !baselineSeal) {
            throw new Error("CHECKPOINT_CONTROLLER_SEAL_MISSING: accepted Java checkpoint receipt requires its external baseline seal")
          }
          return renderCommandTerminalReceipt(name, commandState)
        }
        if (commandState?.policy.refactoring_backend === "idea") {
          assertIdeaVerifyAllowed(commandState.ideaProtocolState, {
            controlGeneration: commandState.control.generation,
            confirmationRequired: commandState.formalCandidateState.confirmationRequired,
          })
        }
        if (javaCheckpoint && !baselineSeal) {
          throw new Error("CHECKPOINT_CONTROLLER_SEAL_MISSING: Java checkpoint verification requires the external baseline seal")
        }
        const deadlineEpochMs = commandState ? commandDeadlineEpochMs(commandState) : undefined
        const attachAutoContinuation = (
          normalized: { output: string; metadata: Record<string, unknown> },
          preparedOutput?: PreparedLoopOutput | null,
        ): void => {
          try {
            const cont = idleRuntime.recordFromBridgeOutput({
              sessionID,
              agent: context?.agent || "",
              directory: context?.directory || "",
              taskKey: makeTaskKey(resolved.projectRoot || "", resolved.smell || "", resolved.location || ""),
              output: normalized.output,
              preparedOutput,
            })
            normalized.metadata.auto_continuation = toJsonSafe({
              enabled: cont.enabled,
              continuation: cont.continuation,
              maxSmellVerifyCycles: cont.maxSmellVerifyCycles,
              generation: cont.generation,
              status: cont.status,
              category: cont.category,
              dispatched: cont.dispatched,
            })
          } catch {
            // Loop bookkeeping must never replace the verification result.
          }
        }
        if (usesCheapGuardProgressGate(resolved, commandState)) {
          const progressResult = await runBridge(worktree, [
            "verify",
            ...commonArgs({ ...resolved, baselineSeal }),
            "--guard-progress-only",
          ], deadlineEpochMs)
          if (commandState?.terminalReceipt) {
            return renderCommandTerminalReceipt(name, commandState)
          }
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
              progressResult.exitCode === 0
              && progressPayload?.schema_version === "smell.guard-progress/v1"
              && progressPayload?.success === false
              && progressPayload?.status === "GUARD_PROGRESS_REQUIRED"
              && progressPayload?.applicable === true
              && progressPayload?.checkpoint_required === true
              && progressPayload?.source_guard_passed === false
              && progressPayload?.ready_for_project_full === false
              && progressPayload?.project_full_executed === false
            )
            const normalized = normalizeToolResult(name, progressResult)
            let preparedOutput: PreparedLoopOutput | null | undefined
            if (commandState) {
              if (progressRequired) {
                preparedOutput = applyGuardProgressDecision(normalized, commandState)
              } else {
                preparedOutput = applyProtocolTerminalDecision(normalized, commandState, {
                  status: "GUARD_PROGRESS_PROTOCOL_INVALID",
                  failureCategory: "GUARD_PROGRESS_PROTOCOL_INVALID",
                  message: "The source Guard progress bridge returned a malformed or unexpected contract.",
                })
              }
              persistAndArmCommandState(sessionID, commandState)
              normalized.metadata.command_loop_state = toJsonSafe(
                commandLoopStateSnapshot(commandState),
              )
            }
            attachAutoContinuation(normalized, preparedOutput)
            return normalized
          }
        }
        const bridgeArgs = ["verify", ...commonArgs({ ...resolved, baselineSeal }), "--output-detail", "decision"]
        if (args.noSnapshot) bridgeArgs.push("--no-snapshot")
        const normalized = normalizeToolResult(name, await runBridge(worktree, bridgeArgs, deadlineEpochMs))
        if (commandState?.terminalReceipt) {
          return renderCommandTerminalReceipt(name, commandState)
        }
        let preparedOutput: PreparedLoopOutput | null | undefined
        if (commandState) {
          preparedOutput = applyCommandLoopDecision(normalized, commandState)
          if (!preparedOutput) {
            preparedOutput = applyProtocolTerminalDecision(normalized, commandState, {
              status: "FORMAL_VERIFY_PROTOCOL_INVALID",
              failureCategory: "FORMAL_VERIFY_PROTOCOL_INVALID",
              message: "The formal verification bridge returned a malformed contract.",
            })
          }
          persistAndArmCommandState(sessionID, commandState)
          normalized.metadata.command_loop_state = toJsonSafe(commandLoopStateSnapshot(commandState))
        }
        // Cheap Guard and formal verification share this exact plugin-owned
        // continuation transport; only batch prompt dispatch is runner-owned.
        attachAutoContinuation(normalized, preparedOutput)
        return normalized
      },
    })

  return {
    tool: {
      smell_verify: verifyTool("Smell verification"),

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
            .describe("Java file path inside the frozen IDEA project root, relative or absolute."),
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
        async execute(args, context) {
          const sessionID = context?.sessionID || ""
          const boundary = resolveIdeaCommandInput(args, sessionID)
          assertIdeaPreviewAllowed(boundary.state.ideaProtocolState, args)
          const deadlineEpochMs = ideaDeadlineForSession(sessionID)
          const rendered = await runIdeaPreviewProtocol({
            worktree: boundary.projectRoot,
            cli: boundary.resolved.ideaRefactorCli,
            runner: (directory, cli, cliArgs) => runIdeaCli(directory, cli, cliArgs, deadlineEpochMs),
            request: {
              projectRoot: boundary.projectRoot,
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
            wrapperMetadata: boundary.resolved.wrapperMetadata,
          })
          const payload = recordValue(JSON.parse(rendered.output))
          if (payload?.protocol === "idea-proposal-v1") {
            recordIdeaPreviewOutcome(boundary.state.ideaProtocolState, args, payload)
            persistIdeaCommandState(boundary)
          }
          return rendered
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
        async execute(args, context) {
          const sessionID = context?.sessionID || ""
          const boundary = resolveIdeaCommandInput(args, sessionID)
          assertIdeaApplyAllowed(boundary.state.ideaProtocolState, args.proposalId)
          const deadlineEpochMs = ideaDeadlineForSession(sessionID)
          const cliArgs = [
            "apply",
            "--project-root",
            boundary.projectRoot,
            "--draft-id",
            args.proposalId,
          ]
          addJson(cliArgs, "--arguments-json", args.arguments)
          addJson(cliArgs, "--decisions-json", args.decisions)
          const startedAt = Date.now()
          const result = await runIdeaCli(
            boundary.projectRoot,
            boundary.resolved.ideaRefactorCli,
            cliArgs,
            deadlineEpochMs,
          )
          const rendered = renderIdeaApplyProtocolResult(
            args.proposalId,
            result,
            args.detail || "compact",
            Date.now() - startedAt,
            boundary.resolved.wrapperMetadata,
          )
          const payload = recordValue(JSON.parse(rendered.output))
          if (!payload) throw new Error("IDEA_APPLY_PROTOCOL_INVALID: apply returned no structured result")
          recordIdeaApplyOutcome(boundary.state.ideaProtocolState, args.proposalId, payload)
          persistIdeaCommandState(boundary)
          return rendered
        },
      }),

      idea_edit: tool({
        description:
          "Apply an IDEA-backed oldString/newString edit only after preview returned an explicit unsupported_target blocker for this command.",
        args: {
          ...ideaShape,
          file: tool.schema.string().describe("Java file path inside the frozen IDEA project root, relative or absolute."),
          oldString: tool.schema
            .string()
            .describe('Exact source block to replace. Must be unique unless replaceAll is true. Use "" only for explicit new-file or whole-file replacement steps.'),
          newString: tool.schema.string().describe("Replacement source block."),
          replaceAll: tool.schema.boolean().optional().describe("Replace every exact occurrence. Do not use for ordinary Java source patches."),
        },
        async execute(args, context) {
          const sessionID = context?.sessionID || ""
          const boundary = resolveIdeaCommandInput(args, sessionID)
          assertIdeaEditAllowed(boundary.state.ideaProtocolState)
          const deadlineEpochMs = ideaDeadlineForSession(sessionID)
          const resolvedFile = resolveIdeaFile(
            args.file,
            boundary.projectRoot,
            String(args.oldString ?? "") === "",
          )
          if (!resolvedFile.ok) return resolvedFile.result
          const cliArgs = [
            "edit",
            "--project-root",
            boundary.projectRoot,
            "--file",
            resolvedFile.file,
            "--old-string",
            args.oldString,
            "--new-string",
            args.newString,
          ]
          if (args.replaceAll) cliArgs.push("--replace-all")
          const rendered = renderIdeaResult(
            "IDEA edit",
            await runIdeaCli(
              boundary.projectRoot,
              boundary.resolved.ideaRefactorCli,
              cliArgs,
              deadlineEpochMs,
            ),
            undefined,
            {
              ...boundary.resolved.wrapperMetadata,
              ...ideaRuntimeMetadata(boundary.projectRoot, boundary.projectRoot),
              postEditProblems:
                "Inspect payload.postEditProblems when present. New local IDEA problems are repair evidence; smell_verify remains the acceptance gate.",
            },
          )
          const payload = recordValue(JSON.parse(rendered.output))
          recordIdeaEditOutcome(boundary.state.ideaProtocolState, payload || {})
          persistIdeaCommandState(boundary)
          return rendered
        },
      }),

      idea_refactor_revert_last_apply: tool({
        description:
          "Revert the most recent successful IDEA apply. This is not for discarding an unapplied proposal.",
        args: {
          ...ideaShape,
        },
        async execute(args, context) {
          const sessionID = context?.sessionID || ""
          const boundary = resolveIdeaCommandInput(args, sessionID)
          assertIdeaRevertAllowed(boundary.state.ideaProtocolState)
          const deadlineEpochMs = ideaDeadlineForSession(sessionID)
          const cliArgs = ["rollback", "--project-root", boundary.projectRoot]
          const rendered = renderIdeaResult(
            "IDEA refactor revert last apply",
            await runIdeaCli(
              boundary.projectRoot,
              boundary.resolved.ideaRefactorCli,
              cliArgs,
              deadlineEpochMs,
            ),
            undefined,
            {
              ...boundary.resolved.wrapperMetadata,
              ...ideaRuntimeMetadata(boundary.projectRoot, boundary.projectRoot),
              rollback_scope: "last_applied",
              warning: "This reverted a previously applied source change, not merely an unapplied proposal.",
            },
          )
          const payload = recordValue(JSON.parse(rendered.output))
          recordIdeaRevertOutcome(boundary.state.ideaProtocolState, payload || {})
          persistIdeaCommandState(boundary)
          return rendered
        },
      }),
    },

    "tool.execute.before": async (input, output) => {
      const sessionID = typeof input.sessionID === "string" ? input.sessionID : ""
      const terminalState = terminalStateForSession(sessionID)
      const terminalMutationTools = new Set([
        "bash",
        "edit",
        "write",
        "patch",
        "apply_patch",
        "task",
        "idea_refactor_apply",
        "idea_edit",
        "idea_refactor_revert_last_apply",
      ])
      if (terminalState && terminalMutationTools.has(input.tool)) {
        throw new Error(
          `SMELL_LOOP_TERMINAL: ${terminalState.terminalReceipt?.terminationReason || "TERMINAL"}. `
          + "This command is frozen; start a new smell-refactor-run command for a new attempt.",
        )
      }
      const commandState = commandBoundaryStateForSession(sessionID)
      if (
        commandState?.formalCandidateState.confirmationRequired
        && terminalMutationTools.has(input.tool)
      ) {
        throw new Error(`SMELL_FRESH_CONFIRMATION_PENDING: ${FRESH_CONFIRMATION_INSTRUCTION}`)
      }
      const ideaTools = new Set([
        "idea_refactor_preview",
        "idea_refactor_apply",
        "idea_edit",
        "idea_refactor_revert_last_apply",
      ])
      if (ideaTools.has(input.tool)) {
        const args = recordValue(output.args) || {}
        const boundary = resolveIdeaCommandInput(args, sessionID)
        if (input.tool === "idea_refactor_preview") {
          assertIdeaPreviewAllowed(boundary.state.ideaProtocolState, args)
        } else if (input.tool === "idea_refactor_apply") {
          assertIdeaApplyAllowed(boundary.state.ideaProtocolState, String(args.proposalId || ""))
        } else if (input.tool === "idea_edit") {
          assertIdeaEditAllowed(boundary.state.ideaProtocolState)
        } else {
          assertIdeaRevertAllowed(boundary.state.ideaProtocolState)
        }
      }
      if (
        commandState?.policy.refactoring_backend === "idea"
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
      if (commandState?.policy.refactoring_backend === "idea") {
        throw new Error(
          "IDEA_BACKEND_SHELL_FORBIDDEN: use IDEA protocol tools and smell_verify; "
          + "bash is disabled for the frozen IDEA command.",
        )
      }
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
          + "smell_verify runs final verification in a disposable worktree after the source Guard passes.",
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
      clearCommandDeadlineTimer(input.sessionID)
      commandDeadlineAbortDispatched.delete(input.sessionID)
      idleRuntime.clearSession(input.sessionID)
      commandLoopStates.delete(input.sessionID)
      commandBaselineSeals.delete(input.sessionID)
      commandSessionMetadata.delete(input.sessionID)
      deleteCommandSessionState(input.sessionID)
      commandResolutionInProgress.add(input.sessionID)
      const commandStartedAt = Date.now()
      let policy: CommandPolicy
      try {
        const result = await runBridge(
          worktree,
          ["resolve-command", "--arguments", input.arguments],
          commandStartedAt + COMMAND_RESOLUTION_DEADLINE_MS,
        )
        policy = parseCommandPolicyResult(result)
      } catch (error) {
        commandResolutionInProgress.delete(input.sessionID)
        throw error
      }
      const identity = controllerIdentityFromPolicy(policy)
      const commandState = newCommandLoopState(policy, "", commandStartedAt)
      const metadata: CommandSessionMetadata = {
        command: input.command,
        agent: input.command === "smell-refactor-run"
          ? "smell-refactor-agent"
          : "java-refactor-agent",
        initialization: policy.checkpoint_required ? "baseline_pending" : "ready",
      }
      commandLoopStates.set(input.sessionID, commandState)
      commandSessionMetadata.set(input.sessionID, metadata)
      commandResolutionInProgress.delete(input.sessionID)
      persistAndArmCommandState(input.sessionID, commandState)
      if (commandState.terminalReceipt) {
        throw new Error("SAMPLE_DEADLINE_REACHED: command policy resolution exhausted the plugin-owned deadline")
      }
      if (policy.refactoring_backend === "idea") {
        const rejectIdeaPrecheck = (message: string): never => {
          const normalized = normalizeToolResult("IDEA command precheck", {
            exitCode: 1,
            stdout: "",
            stderr: message,
            json: null,
          })
          applyProtocolTerminalDecision(normalized, commandState, {
            status: "IDEA_PRECHECK_FAILED",
            failureCategory: "IDEA_PRECHECK_FAILED",
            message,
          })
          persistAndArmCommandState(input.sessionID, commandState)
          throw new Error(`IDEA_PRECHECK_FAILED: ${message}`)
        }
        if (input.command !== "java-refactor-run") {
          rejectIdeaPrecheck("The IDEA backend requires java-refactor-run.")
        }
        const frozenProjectRoot = path.resolve(policy.identity.project_root)
        const ideaInput = resolveIdeaInput({
          ideaProjectRoot: frozenProjectRoot,
          language: "java",
        })
        if (!ideaInput.ok) {
          rejectIdeaPrecheck("The frozen IDEA project root could not be resolved.")
        }
        const deadlineEpochMs = commandDeadlineEpochMs(commandState)
        const remainingSeconds = Math.max(1, Math.ceil((deadlineEpochMs - Date.now()) / 1000))
        const precheck = await runIdeaCli(
          frozenProjectRoot,
          ideaInput.ideaRefactorCli,
          [
            "ensure-service",
            "--project-root",
            frozenProjectRoot,
            "--open",
            "--timeout",
            String(remainingSeconds),
            "--poll-interval",
            "1",
          ],
          deadlineEpochMs,
        )
        if (commandState.terminalReceipt) {
          throw new Error("SAMPLE_DEADLINE_REACHED: IDEA service precheck exhausted the plugin-owned deadline")
        }
        const precheckPayload = recordValue(precheck.json)
        if (precheck.exitCode !== 0 || precheckPayload?.status !== "ok") {
          rejectIdeaPrecheck("The IDEA service did not become ready before model execution.")
        }
      }
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
        ], commandDeadlineEpochMs(commandState))
        if (commandState.terminalReceipt) {
          throw new Error("SAMPLE_DEADLINE_REACHED: baseline capture exceeded the plugin-owned deadline")
        }
        const baselinePayload = baselineResult.json as Record<string, unknown> | null
        if (baselineResult.exitCode !== 0 || !baselinePayload || baselinePayload.success !== true) {
          const normalized = normalizeToolResult("Checkpoint baseline capture", baselineResult)
          applyProtocolTerminalDecision(normalized, commandState, {
            status: "CHECKPOINT_BASELINE_CAPTURE_FAILED",
            failureCategory: "CHECKPOINT_BASELINE_CAPTURE_FAILED",
            message: "The plugin-owned checkpoint baseline could not be captured.",
          })
          persistAndArmCommandState(input.sessionID, commandState)
          throw new Error(
            `CHECKPOINT_BASELINE_CAPTURE_FAILED: ${truncateText(baselineResult.stderr || baselineResult.stdout)}`,
          )
        }
        const baselineSeal = String(baselinePayload.baseline_seal || "").trim()
        if (!baselineSeal) {
          const normalized = normalizeToolResult("Checkpoint baseline capture", baselineResult)
          applyProtocolTerminalDecision(normalized, commandState, {
            status: "CHECKPOINT_BASELINE_CAPTURE_FAILED",
            failureCategory: "CHECKPOINT_BASELINE_CAPTURE_FAILED",
            message: "The plugin-owned checkpoint baseline seal is missing.",
          })
          persistAndArmCommandState(input.sessionID, commandState)
          throw new Error("CHECKPOINT_BASELINE_CAPTURE_FAILED: controller baseline seal is missing")
        }
        commandBaselineSeals.set(input.sessionID, baselineSeal)
        commandState.targetIdentityContext = checkpointTargetIdentityPrompt(identity.smell, baselinePayload)
        metadata.initialization = "ready"
        persistAndArmCommandState(input.sessionID, commandState)
      }
      if (isProtectedProjectFullCandidateShellSession(commandState)) {
        markProtectedShellLineage(input.sessionID)
      }
      rehydrateIdleFromControl(input.sessionID, commandState, metadata)
    },

    "experimental.chat.system.transform": async (input, output) => {
      const sessionID = typeof input.sessionID === "string" ? input.sessionID : ""
      if (!sessionID) return
      const state = restoreCommandState(sessionID)
      if (!state) return
      const context = commandControllerSystemContext(
        state.policy,
        state.targetIdentityContext,
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
            writeCommandSessionLineage(sessionID, parentID)
            const parentState = commandBoundaryStateForSession(parentID)
            if (
              protectedShellLineage.has(parentID)
              || isProtectedProjectFullCandidateShellSession(parentState)
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
            restoreCommandState(sessionID)
            idleRuntime.handleIdle(sessionID)
          }
          return
        }
        if (event.type === "session.deleted") {
          const sessionID = (event as { properties?: { info?: { id?: string } } }).properties?.info?.id
          if (typeof sessionID === "string" && sessionID) {
            idleRuntime.handleSessionDeleted(sessionID)
            clearCommandDeadlineTimer(sessionID)
            commandDeadlineAbortDispatched.delete(sessionID)
            commandLoopStates.delete(sessionID)
            commandBaselineSeals.delete(sessionID)
            commandSessionMetadata.delete(sessionID)
            commandSessionParents.delete(sessionID)
            protectedShellLineage.delete(sessionID)
            deleteCommandSessionState(sessionID)
            deleteCommandSessionLineage(sessionID)
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
      for (const sessionID of commandDeadlineTimers.keys()) clearCommandDeadlineTimer(sessionID)
      commandDeadlineAbortDispatched.clear()
      commandResolutionInProgress.clear()
      commandLoopStates.clear()
      commandBaselineSeals.clear()
      commandSessionMetadata.clear()
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
  newIdeaProtocolState,
  recordIdeaPreviewOutcome,
  assertIdeaApplyAllowed,
  recordIdeaApplyOutcome,
  assertIdeaEditAllowed,
  recordIdeaEditOutcome,
  assertIdeaVerifyAllowed,
  recordIdeaVerifyOutcome,
  assertIdeaRevertAllowed,
  recordIdeaRevertOutcome,
  ideaProtocolReceipt,
  checkpointTargetIdentityPrompt,
  commandControllerSystemContext,
  commandLoopStateSnapshot,
  restoreCommandLoopState,
  commandDeadlineEpochMs,
  commandSessionStateRoot,
  commandSessionStateFile,
  commandSessionLineageFile,
  writeCommandSessionState,
  readCommandSessionState,
  deleteCommandSessionState,
  writeCommandSessionLineage,
  readCommandSessionParent,
  deleteCommandSessionLineage,
  runBridge,
  runIdeaCli,
  INITIAL_VERIFY_INSTRUCTION,
  isJavaCheckpointIdentity,
  usesCheapGuardProgressGate,
  applyCommandLoopDecision,
  applyFormalVerificationConsistency,
  applyGuardProgressDecision,
  guardProgressObservation,
  renderCommandTerminalReceipt,
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

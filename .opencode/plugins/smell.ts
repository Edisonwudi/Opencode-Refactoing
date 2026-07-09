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

async function runBridge(worktree: string, args: string[]): Promise<BridgeResult> {
  return await new Promise((resolve) => {
    const child = spawn("python3", [bridgeFile, ...args], {
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
    child.on("close", (code) => {
      let json: unknown = null
      try {
        json = JSON.parse(stdout)
      } catch {
        json = null
      }
      resolve({ exitCode: code ?? 1, stdout, stderr, json })
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

function renderResult(title: string, result: BridgeResult) {
  const output = result.json === null ? result.stdout : JSON.stringify(result.json, null, 2)
  return {
    title,
    output,
    metadata: {
      exitCode: result.exitCode,
      stderr: result.stderr,
    },
  }
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
) {
  const operationAvailable = operationMatches(result.json, expectedOperation)
  return {
    title,
    output: JSON.stringify(
      {
        payload: result.json,
        wrapper: {
          exit_code: result.exitCode,
          stderr: result.stderr,
          argv_preview: result.argv,
          expected_operation: expectedOperation || "",
          operation_available: operationAvailable,
          ...extraWrapper,
        },
      },
      null,
      2,
    ),
    metadata: {
      exitCode: result.exitCode,
      stderr: result.stderr,
    },
  }
}

export const SmellPlugin: Plugin = async ({ worktree }) => {
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
      async execute(args) {
        const resolved = withBatchDefaults(args)
        const bridgeArgs = ["verify", ...commonArgs(resolved)]
        if (args.noSnapshot) bridgeArgs.push("--no-snapshot")
        return renderResult(name, await runBridge(worktree, bridgeArgs))
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
  }
}

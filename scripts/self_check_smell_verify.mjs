#!/usr/bin/env node
import { spawn } from "node:child_process"
import { mkdtemp, mkdir, readFile, rm, symlink, writeFile } from "node:fs/promises"
import { existsSync } from "node:fs"
import os from "node:os"
import path from "node:path"
import { fileURLToPath, pathToFileURL } from "node:url"

const scriptFile = fileURLToPath(import.meta.url)
const root = path.resolve(path.dirname(scriptFile), "..")
const pluginFile = path.join(root, ".opencode", "plugins", "smell.ts")
const bridgeFile = path.join(root, "runtime", "python", "bridge", "smell_bridge.py")
const datasetRunnerFile = path.join(root, "scripts", "run_smell_dataset.py")

function parseArgs(argv) {
  const options = {
    requireDataset: false,
    ideaProtocolOnly: false,
    dataset: process.env.SELF_CHECK_DATASET || "/opt/dataset/java/delivery_schema/mysterious_name.csv",
    sampleId: process.env.SELF_CHECK_SAMPLE_ID || "8",
    model: process.env.SELF_CHECK_MODEL || "zai/glm-4.7",
  }
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index]
    if (arg === "--require-dataset") {
      options.requireDataset = true
    } else if (arg === "--idea-protocol-only") {
      options.ideaProtocolOnly = true
    } else if (arg === "--dataset-smoke-dataset") {
      options.dataset = argv[++index] || ""
    } else if (arg === "--dataset-smoke-sample-id") {
      options.sampleId = argv[++index] || ""
    } else if (arg === "--dataset-smoke-model") {
      options.model = argv[++index] || ""
    } else {
      throw new SelfCheckError("argument_parse", `Unknown self-check argument: ${arg}`)
    }
  }
  return options
}

class SelfCheckError extends Error {
  constructor(stage, message, details = {}) {
    super(message)
    this.stage = stage
    this.details = details
  }
}

function cleanSmellIdentityEnv(env) {
  const cleaned = { ...env }
  for (const key of [
    "SMELL_PROJECT_ROOT",
    "SMELL_CANONICAL_PROJECT_ROOT",
    "SMELL_LANGUAGE",
    "SMELL_SMELL",
    "SMELL_LOCATION",
    "SMELL_EVIDENCE",
    "SMELL_VERIFICATION_MODE",
    "SMELL_SAMPLE_TEST_LOCATION",
    "SMELL_SAMPLE_TEST_COMMAND",
  ]) {
    delete cleaned[key]
  }
  cleaned.PYTHONDONTWRITEBYTECODE = "1"
  return cleaned
}

function run(command, args, options = {}) {
  return new Promise((resolve) => {
    const child = spawn(command, args, {
      cwd: options.cwd || root,
      env: options.env || process.env,
      text: true,
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
      resolve({ command, args, cwd: options.cwd || root, exitCode: 1, stdout, stderr: stderr || error.message })
    })
    child.on("close", (code) => {
      resolve({ command, args, cwd: options.cwd || root, exitCode: code ?? 1, stdout, stderr })
    })
  })
}

function parseJson(stage, text) {
  try {
    return JSON.parse(text)
  } catch (error) {
    throw new SelfCheckError(stage, "Output was not valid JSON.", {
      error: error instanceof Error ? error.message : String(error),
      outputPreview: String(text || "").slice(0, 1200),
    })
  }
}

function parseLeadingJsonObject(stage, text) {
  const raw = String(text || "")
  const start = raw.indexOf("{")
  if (start < 0) {
    throw new SelfCheckError(stage, "Output did not contain a JSON object.", {
      outputPreview: raw.slice(0, 1200),
    })
  }
  let depth = 0
  let inString = false
  let escaped = false
  for (let index = start; index < raw.length; index += 1) {
    const char = raw[index]
    if (escaped) {
      escaped = false
      continue
    }
    if (char === "\\") {
      escaped = true
      continue
    }
    if (char === '"') {
      inString = !inString
      continue
    }
    if (inString) continue
    if (char === "{") depth += 1
    if (char === "}") depth -= 1
    if (depth === 0) {
      return {
        json: parseJson(stage, raw.slice(start, index + 1)),
        rest: raw.slice(index + 1),
      }
    }
  }
  throw new SelfCheckError(stage, "Output JSON object was not closed.", {
    outputPreview: raw.slice(0, 1200),
  })
}

async function readPackageVersion(packageRoot, packageName) {
  const packageJson = path.join(packageRoot, "node_modules", packageName, "package.json")
  if (!existsSync(packageJson)) return ""
  const parsed = JSON.parse(await readFile(packageJson, "utf8"))
  return String(parsed.version || "")
}

async function runDatasetSmokeSelfCheck(options) {
  if (!existsSync(options.dataset)) {
    throw new SelfCheckError("dataset_smoke_input", "Dataset smoke CSV was not found.", {
      dataset: options.dataset,
    })
  }
  const runsRoot = await mkdtemp(path.join(os.tmpdir(), "smell-verify-self-check-runs-"))
  try {
    const result = await run(
      "python3",
      [
        datasetRunnerFile,
        "--dataset",
        options.dataset,
        "--sample-id",
        options.sampleId,
        "--model",
        options.model,
        "--verification-mode",
        "project_full",
        "--runs-root",
        runsRoot,
        "--dry-run",
      ],
      { cwd: root, env: cleanSmellIdentityEnv(process.env) },
    )
    if (result.exitCode !== 0) {
      throw new SelfCheckError("dataset_smoke_runner", "Dataset dry-run exited non-zero.", result)
    }
    const { json: manifest, rest } = parseLeadingJsonObject("dataset_smoke_manifest", result.stdout)
    const sampleLines = rest
      .split(/\r?\n/)
      .map((line) => line.trim())
      .filter(Boolean)
    const sampleLine = sampleLines.find((line) => line.startsWith(`${options.sampleId}\t`)) || ""
    if (manifest.selected_count !== 1 || manifest.dry_run !== true || !sampleLine) {
      throw new SelfCheckError("dataset_smoke_result", "Dataset dry-run did not select exactly the requested sample.", {
        manifest,
        sampleLines,
        expectedSampleId: options.sampleId,
      })
    }
    return {
      dataset: manifest.dataset,
      sampleId: options.sampleId,
      model: manifest.model,
      selectedCount: manifest.selected_count,
      verificationMode: manifest.verification_mode,
      dryRun: manifest.dry_run,
      sampleLine,
    }
  } finally {
    await rm(runsRoot, { recursive: true, force: true })
  }
}

async function makeFixtureProject() {
  const fixtureRoot = await mkdtemp(path.join(os.tmpdir(), "smell-verify-self-check-project-"))
  const sourceDir = path.join(fixtureRoot, "src", "main", "java")
  await mkdir(sourceDir, { recursive: true })
  await writeFile(
    path.join(sourceDir, "SelfCheckSample.java"),
    [
      "public class SelfCheckSample {",
      "  public int add(int left, int right) {",
      "    int total = left + right;",
      ...Array.from({ length: 61 }, (_, index) => `    total += ${index + 1};`),
      "    return total;",
      "  }",
      "}",
      "",
    ].join("\n"),
    "utf8",
  )
  await writeFile(
    path.join(fixtureRoot, "projects.yaml"),
    [
      "projects:",
      `- root: ${JSON.stringify(fixtureRoot)}`,
      "  language: java",
      "  build:",
      "    command: \"true\"",
      "  test:",
      "    command: \"true\"",
      "",
    ].join("\n"),
    "utf8",
  )
  for (const args of [
    ["init", "-q"],
    ["add", "src/main/java/SelfCheckSample.java", "projects.yaml"],
    ["-c", "user.name=smell-self-check", "-c", "user.email=self-check@example.invalid", "commit", "-qm", "baseline"],
  ]) {
    const result = await run("git", args, { cwd: fixtureRoot })
    if (result.exitCode !== 0) {
      throw new SelfCheckError("fixture_git", "Unable to create immutable checkpoint fixture baseline.", result)
    }
  }
  return fixtureRoot
}

async function runBridgeSelfCheck(fixtureRoot, artifactRoot) {
  const env = cleanSmellIdentityEnv(process.env)
  env.SMELL_ARTIFACT_ROOT = artifactRoot
  const sourceFile = path.join(fixtureRoot, "src", "main", "java", "SelfCheckSample.java")
  const originalSource = await readFile(sourceFile, "utf8")
  const identityArgs = [
    "--project-root",
    fixtureRoot,
    "--language",
    "java",
    "--smell",
    "long_method",
    "--location",
    "src/main/java/SelfCheckSample.java:2",
    "--projects",
    path.join(fixtureRoot, "projects.yaml"),
    "--verification-mode",
    "project_full",
  ]
  const baseline = await run(
    "python3",
    [bridgeFile, "capture-baseline", ...identityArgs],
    { cwd: fixtureRoot, env },
  )
  if (baseline.exitCode !== 0) {
    throw new SelfCheckError("bridge_capture_baseline", "Unable to capture fixture baseline.", baseline)
  }
  const baselineSeal = String(
    parseJson("bridge_capture_baseline", baseline.stdout).baseline_seal || "",
  )
  if (!baselineSeal) {
    throw new SelfCheckError(
      "bridge_capture_baseline",
      "Fixture baseline did not return its controller seal.",
      baseline,
    )
  }
  await writeFile(
    sourceFile,
    [
      "public class SelfCheckSample {",
      "  public void add(int left, int right) {}",
      "}",
      "",
    ].join("\n"),
    "utf8",
  )
  const result = await run(
    "python3",
    [
      bridgeFile,
      "verify",
      ...identityArgs,
      "--baseline-seal",
      baselineSeal,
      "--no-snapshot",
    ],
    { cwd: fixtureRoot, env },
  )
  await writeFile(sourceFile, originalSource, "utf8")
  if (result.exitCode !== 0) {
    throw new SelfCheckError("bridge_verify", "smell_bridge.py verify exited non-zero.", result)
  }
  const payload = parseJson("bridge_verify_json", result.stdout)
  if (payload.status !== "PASS" || payload.success !== true) {
    throw new SelfCheckError("bridge_verify_status", "Bridge verification did not return PASS.", {
      status: payload.status,
      success: payload.success,
      payload,
    })
  }
  return {
    exitCode: result.exitCode,
    status: payload.status,
    success: payload.success,
    artifactKeys: Object.keys(payload.artifacts || {}).sort(),
  }
}

async function compilePluginForSelfCheck(tempRoot) {
  const ts = await import("typescript").catch((error) => {
    throw new SelfCheckError("typescript_import", "Unable to import TypeScript. Run npm ci before self-check.", {
      error: error instanceof Error ? error.message : String(error),
    })
  })
  const source = await readFile(pluginFile, "utf8")
  const compiled = ts.transpileModule(source, {
    compilerOptions: {
      target: ts.ScriptTarget.ES2022,
      module: ts.ModuleKind.ES2022,
      moduleResolution: ts.ModuleResolutionKind.Node10,
      esModuleInterop: true,
      skipLibCheck: true,
    },
    fileName: pluginFile,
    reportDiagnostics: true,
  })
  const diagnostics = (compiled.diagnostics || []).filter((item) => item.category === ts.DiagnosticCategory.Error)
  if (diagnostics.length) {
    throw new SelfCheckError("plugin_transpile", "TypeScript transpile reported diagnostics.", {
      diagnostics: diagnostics.map((item) => String(item.messageText)),
    })
  }

  const opencodePluginDir = path.join(tempRoot, ".opencode", "plugins")
  await mkdir(opencodePluginDir, { recursive: true })
  await symlink(path.join(root, "runtime"), path.join(tempRoot, "runtime"), "dir")
  if (existsSync(path.join(root, "node_modules"))) {
    await symlink(path.join(root, "node_modules"), path.join(tempRoot, "node_modules"), "dir")
  }
  const opencodeNodeModules = path.join(root, ".opencode", "node_modules")
  if (existsSync(opencodeNodeModules)) {
    await mkdir(path.join(tempRoot, ".opencode"), { recursive: true })
    await symlink(opencodeNodeModules, path.join(tempRoot, ".opencode", "node_modules"), "dir")
  }
  const compiledFile = path.join(opencodePluginDir, "smell.self-check.mjs")
  await writeFile(compiledFile, compiled.outputText, "utf8")
  return compiledFile
}

function normalizeToolResult(result) {
  let shape = typeof result
  let output = ""
  let title = ""
  let metadata = null
  if (typeof result === "string") {
    output = result
    shape = "string"
  } else if (result && typeof result === "object") {
    output = result.output
    title = typeof result.title === "string" ? result.title : ""
    metadata = result.metadata && typeof result.metadata === "object" ? result.metadata : null
    shape = "object"
  }
  if (typeof output !== "string") {
    throw new SelfCheckError("tool_result_shape", "smell_verify result output is not a string.", {
      resultType: typeof result,
      outputType: typeof output,
      keys: result && typeof result === "object" ? Object.keys(result) : [],
    })
  }
  const lineCount = output.split(/\r?\n/).length
  const parsed = parseJson("tool_result_json", output)
  if (parsed.status !== "PASS" || parsed.success !== true) {
    throw new SelfCheckError("tool_result_status", "smell_verify tool output did not return PASS.", {
      status: parsed.status,
      success: parsed.success,
      parsed,
    })
  }
  return {
    shape,
    title,
    metadata,
    outputType: typeof output,
    outputLength: output.length,
    lineCount,
    status: parsed.status,
    success: parsed.success,
    loop: parsed.loop || null,
    artifactKeys: Object.keys(parsed.artifacts || {}).sort(),
  }
}

function verifyNormalizedFailureResult(scenario, normalized, maxLen) {
  const errors = []
  if (!normalized || typeof normalized !== "object" || Array.isArray(normalized)) {
    throw new SelfCheckError("plugin_normalize_shape", `Scenario '${scenario.name}' did not return an object.`, {
      normalizedType: typeof normalized,
    })
  }
  if (typeof normalized.title !== "string") errors.push("title is not a string")
  if (typeof normalized.output !== "string") {
    throw new SelfCheckError(
      "plugin_normalize_output_type",
      `Scenario '${scenario.name}' output is not a string; OpenCode's text.split would throw.`,
      { outputType: typeof normalized.output },
    )
  }
  let lines
  try {
    lines = normalized.output.split("\n")
  } catch (error) {
    throw new SelfCheckError(
      "plugin_normalize_split",
      `Scenario '${scenario.name}' output.split('\\n') threw: ${error instanceof Error ? error.message : String(error)}`,
      {},
    )
  }
  if (!Array.isArray(lines)) {
    throw new SelfCheckError("plugin_normalize_split", `Scenario '${scenario.name}' output.split did not return an array.`, {})
  }
  let parsed
  try {
    parsed = JSON.parse(normalized.output)
  } catch (error) {
    throw new SelfCheckError(
      "plugin_normalize_json",
      `Scenario '${scenario.name}' output is not JSON-parseable: ${error instanceof Error ? error.message : String(error)}`,
      { outputPreview: normalized.output.slice(0, 400) },
    )
  }
  if (typeof parsed.success !== "boolean") errors.push("parsed.success is not boolean")
  if (typeof parsed.status !== "string") errors.push("parsed.status is not string")
  if (!parsed.bridge || typeof parsed.bridge !== "object") {
    errors.push("parsed.bridge missing")
  } else if (typeof parsed.bridge.exitCode !== "number") {
    errors.push("parsed.bridge.exitCode is not a number")
  }
  if (scenario.expectStatus && parsed.status !== scenario.expectStatus) {
    errors.push(`parsed.status expected ${scenario.expectStatus} got ${parsed.status}`)
  }
  if ("expectSuccess" in scenario && parsed.success !== scenario.expectSuccess) {
    errors.push(`parsed.success expected ${scenario.expectSuccess} got ${parsed.success}`)
  }
  if (!normalized.metadata || typeof normalized.metadata !== "object" || Array.isArray(normalized.metadata)) {
    errors.push("metadata is not a plain object")
  } else {
    if (typeof normalized.metadata.exitCode !== "number") errors.push("metadata.exitCode is not a number")
    if (typeof normalized.metadata.stderr === "string" && normalized.metadata.stderr.length > maxLen + 64) {
      errors.push(`metadata.stderr too long: ${normalized.metadata.stderr.length}`)
    }
    try {
      JSON.stringify(normalized.metadata)
    } catch (error) {
      errors.push(`metadata is not JSON-serializable: ${error instanceof Error ? error.message : String(error)}`)
    }
  }
  if (errors.length) {
    throw new SelfCheckError("plugin_normalize_assertions", `Scenario '${scenario.name}' failed normalization assertions.`, {
      errors,
      scenario: scenario.name,
      parsedPreview: (() => {
        try {
          return JSON.parse(JSON.stringify(parsed))
        } catch {
          return null
        }
      })(),
    })
  }
  return {
    name: scenario.name,
    titleType: typeof normalized.title,
    outputType: typeof normalized.output,
    outputLength: normalized.output.length,
    lineCount: lines.length,
    parsedStatus: parsed.status,
    parsedSuccess: parsed.success,
    bridgeExitCode: parsed.bridge ? parsed.bridge.exitCode : null,
    metadataExitCode: typeof normalized.metadata.exitCode === "number" ? normalized.metadata.exitCode : null,
    metadataStderrLength: typeof normalized.metadata.stderr === "string" ? normalized.metadata.stderr.length : null,
  }
}

async function runPluginNormalizeSelfCheck(pluginModule) {
  const hooks = pluginModule.SmellPlugin?.__selfTest || pluginModule.default?.__selfTest
  if (!hooks || typeof hooks !== "object") {
    throw new SelfCheckError("plugin_self_test_hooks", "Plugin does not expose SmellPlugin.__selfTest.", {
      exports: Object.keys(pluginModule),
    })
  }
  if (typeof hooks.normalizeToolResult !== "function" || typeof hooks.buildBridgeOutputPayload !== "function") {
    throw new SelfCheckError("plugin_self_test_hooks", "Plugin __selfTest is missing required helpers.", {
      keys: Object.keys(hooks).sort(),
    })
  }
  const maxLen = Number(hooks.MAX_STDOUT_STDERR_LEN) || 4000
  const scenarios = [
    {
      name: "bridge_non_json_stdout_nonzero_exit",
      bridgeResult: {
        exitCode: 1,
        stdout: "Traceback (most recent call last):\n  File x\nImportError: boom",
        stderr: "ImportError: boom",
        json: null,
      },
    },
    {
      name: "bridge_empty_stdout_stderr_only",
      bridgeResult: {
        exitCode: 2,
        stdout: "",
        stderr: "argparse: error: invalid choice 'badcommand'",
        json: null,
      },
    },
    {
      name: "bridge_json_failure_payload_nonzero_exit",
      bridgeResult: {
        exitCode: 1,
        stdout: '{"success":false,"status":"SMELL_GUARD_FAILED","error":"x"}',
        stderr: "",
        json: { success: false, status: "SMELL_GUARD_FAILED", error: "x" },
      },
      expectStatus: "SMELL_GUARD_FAILED",
      expectSuccess: false,
    },
    {
      name: "bridge_json_success_payload",
      bridgeResult: {
        exitCode: 0,
        stdout: '{"success":true,"status":"PASS"}',
        stderr: "",
        json: { success: true, status: "PASS" },
      },
      expectStatus: "PASS",
      expectSuccess: true,
    },
    {
      name: "bridge_huge_stdio_truncated",
      bridgeResult: {
        exitCode: 1,
        stdout: "S".repeat(maxLen * 4),
        stderr: "E".repeat(maxLen * 4),
        json: null,
      },
    },
  ]
  const results = []
  for (const scenario of scenarios) {
    const normalized = hooks.normalizeToolResult(`test:${scenario.name}`, scenario.bridgeResult)
    results.push(verifyNormalizedFailureResult(scenario, normalized, maxLen))
  }
  const ideaResults = await runPluginIdeaResultSelfCheck(hooks, maxLen)
  const ideaProposalResults = await runPluginIdeaProposalSelfCheck(hooks)
  return { hookKeys: Object.keys(hooks).sort(), maxLen, scenarios: results, ideaResults, ideaProposalResults }
}

async function runPluginIdeaResultSelfCheck(hooks, maxLen) {
  if (typeof hooks.renderIdeaResult !== "function") {
    throw new SelfCheckError("plugin_self_test_hooks", "Plugin __selfTest is missing renderIdeaResult.", {
      keys: Object.keys(hooks).sort(),
    })
  }
  const scenarios = [
    {
      name: "idea_non_json_stdout_nonzero_exit",
      ideaResult: {
        exitCode: 1,
        stdout: "stacktrace text",
        stderr: "IDEA CLI crashed",
        json: { status: "failed", diagnostics: [{ code: "IDEA_CLI_OUTPUT_PARSE_FAILED", summary: "x" }], stdout: "stacktrace text" },
        argv: ["locate", "--project-root", "/p"],
      },
      expectSuccess: false,
      expectTransportSuccess: false,
      expectComplete: false,
    },
    {
      name: "idea_success_payload",
      ideaResult: {
        exitCode: 0,
        stdout: '{"status":"ok","operation":"extract:method"}',
        stderr: "",
        json: { status: "ok", operation: "extract:method" },
        argv: ["locate", "--project-root", "/p"],
      },
      expectSuccess: true,
      expectTransportSuccess: true,
      expectComplete: true,
    },
    {
      name: "idea_needs_decision_nonterminal",
      ideaResult: {
        exitCode: 0,
        stdout: '{"status":"needs_decision"}',
        stderr: "",
        json: {
          status: "needs_decision",
          operation: "extract:method",
          nextCliCommandExample: { action: "apply", argumentsJson: {}, decisionsJson: { scope: { choice: "selection_0" } } },
        },
        argv: ["prepare", "--project-root", "/p", "--operation", "extract:method"],
      },
      expectSuccess: false,
      expectTransportSuccess: true,
      expectComplete: false,
      expectActionRequired: "decision",
      expectNextAction: "apply",
    },
    {
      name: "idea_needs_more_info_nonterminal",
      ideaResult: {
        exitCode: 0,
        stdout: '{"status":"needs_more_info"}',
        stderr: "",
        json: {
          status: "needs_more_info",
          operation: "extract:method",
          nextCliCommandExample: { action: "prepare", argumentsJson: { newName: "extractBlock" }, decisionsJson: {} },
        },
        argv: ["prepare", "--project-root", "/p", "--operation", "extract:method"],
      },
      expectSuccess: false,
      expectTransportSuccess: true,
      expectComplete: false,
      expectActionRequired: "input",
      expectNextAction: "prepare",
    },
    {
      name: "idea_empty_payload_nonzero_exit",
      ideaResult: {
        exitCode: 1,
        stdout: "",
        stderr: "boom",
        json: null,
        argv: ["apply", "--project-root", "/p"],
      },
      expectSuccess: false,
      expectTransportSuccess: false,
      expectComplete: false,
    },
    {
      name: "idea_huge_stderr_truncated",
      ideaResult: {
        exitCode: 1,
        stdout: "x".repeat(maxLen * 4),
        stderr: "E".repeat(maxLen * 4),
        json: null,
        argv: ["edit"],
      },
      expectSuccess: false,
      expectTransportSuccess: false,
      expectComplete: false,
    },
  ]
  const results = []
  for (const scenario of scenarios) {
    const rendered = hooks.renderIdeaResult(`test:${scenario.name}`, scenario.ideaResult, "extract:method", {
      language: "java",
      project_root: "/p",
    })
    const errors = []
    if (!rendered || typeof rendered !== "object" || Array.isArray(rendered)) {
      throw new SelfCheckError("plugin_idea_result_shape", `IDEA scenario '${scenario.name}' did not return an object.`, {})
    }
    if (typeof rendered.title !== "string") errors.push("title is not a string")
    if (typeof rendered.output !== "string") {
      throw new SelfCheckError(
        "plugin_idea_result_output",
        `IDEA scenario '${scenario.name}' output is not a string; OpenCode's text.split would throw.`,
        { outputType: typeof rendered.output },
      )
    }
    let lines
    try {
      lines = rendered.output.split("\n")
    } catch (error) {
      throw new SelfCheckError(
        "plugin_idea_result_split",
        `IDEA scenario '${scenario.name}' output.split threw: ${error instanceof Error ? error.message : String(error)}`,
        {},
      )
    }
    let parsed
    try {
      parsed = JSON.parse(rendered.output)
    } catch (error) {
      throw new SelfCheckError(
        "plugin_idea_result_json",
        `IDEA scenario '${scenario.name}' output is not JSON-parseable: ${error instanceof Error ? error.message : String(error)}`,
        { outputPreview: rendered.output.slice(0, 400) },
      )
    }
    if (typeof parsed.success !== "boolean") errors.push("parsed.success is not boolean")
    if (typeof parsed.transport_success !== "boolean") errors.push("parsed.transport_success is not boolean")
    if (typeof parsed.complete !== "boolean") errors.push("parsed.complete is not boolean")
    if (typeof parsed.action_required !== "string") errors.push("parsed.action_required is not a string")
    if (typeof parsed.next_action !== "string") errors.push("parsed.next_action is not a string")
    if (typeof parsed.status !== "string" || !parsed.status) errors.push("parsed.status is missing")
    if (!parsed.wrapper || typeof parsed.wrapper !== "object") {
      errors.push("parsed.wrapper missing")
    } else {
      if (typeof parsed.wrapper.exit_code !== "number") errors.push("wrapper.exit_code is not a number")
      if (typeof parsed.wrapper.stderr !== "string" || parsed.wrapper.stderr.length > maxLen + 64) {
        errors.push("wrapper.stderr was not truncated")
      }
    }
    if (parsed.success !== scenario.expectSuccess) {
      errors.push(`success expected ${scenario.expectSuccess} got ${parsed.success}`)
    }
    if (parsed.transport_success !== scenario.expectTransportSuccess) {
      errors.push(`transport_success expected ${scenario.expectTransportSuccess} got ${parsed.transport_success}`)
    }
    if (parsed.complete !== scenario.expectComplete) {
      errors.push(`complete expected ${scenario.expectComplete} got ${parsed.complete}`)
    }
    if (parsed.action_required !== (scenario.expectActionRequired || "")) {
      errors.push(`action_required expected '${scenario.expectActionRequired || ""}' got '${parsed.action_required}'`)
    }
    if (parsed.next_action !== (scenario.expectNextAction || "")) {
      errors.push(`next_action expected '${scenario.expectNextAction || ""}' got '${parsed.next_action}'`)
    }
    if (!rendered.metadata || typeof rendered.metadata !== "object" || Array.isArray(rendered.metadata)) {
      errors.push("metadata is not a plain object")
    } else {
      if (typeof rendered.metadata.exitCode !== "number") errors.push("metadata.exitCode is not a number")
      if (typeof rendered.metadata.stderr === "string" && rendered.metadata.stderr.length > maxLen + 64) {
        errors.push("metadata.stderr too long")
      }
      try {
        JSON.stringify(rendered.metadata)
      } catch (error) {
        errors.push(`metadata not JSON-serializable: ${error instanceof Error ? error.message : String(error)}`)
      }
    }
    if (errors.length) {
      throw new SelfCheckError("plugin_idea_result_assertions", `IDEA scenario '${scenario.name}' failed assertions.`, {
        errors,
        parsedPreview: (() => {
          try {
            return JSON.parse(JSON.stringify(parsed))
          } catch {
            return null
          }
        })(),
      })
    }
    results.push({
      name: scenario.name,
      titleType: typeof rendered.title,
      outputType: typeof rendered.output,
      outputLength: rendered.output.length,
      lineCount: lines.length,
      parsedStatus: parsed.status,
      parsedSuccess: parsed.success,
      parsedTransportSuccess: parsed.transport_success,
      parsedComplete: parsed.complete,
      parsedActionRequired: parsed.action_required,
      parsedNextAction: parsed.next_action,
      wrapperExitCode: parsed.wrapper ? parsed.wrapper.exit_code : null,
      metadataExitCode: typeof rendered.metadata.exitCode === "number" ? rendered.metadata.exitCode : null,
      metadataStderrLength: typeof rendered.metadata.stderr === "string" ? rendered.metadata.stderr.length : null,
    })
  }
  return results
}

async function runPluginIdeaProposalSelfCheck(hooks) {
  for (const name of ["runIdeaPreviewProtocol", "renderIdeaApplyProtocolResult"]) {
    if (typeof hooks[name] !== "function") {
      throw new SelfCheckError("plugin_self_test_hooks", `Plugin __selfTest is missing ${name}.`, {
        keys: Object.keys(hooks).sort(),
      })
    }
  }
  const calls = []
  const runner = async (_worktree, _cli, argv) => {
    calls.push(argv)
    if (argv[0] === "locate") {
      return {
        exitCode: 0,
        stdout: "",
        stderr: "",
        argv,
        json: {
          status: "ok",
          draftId: "draft-1",
          resolvedContext: { stableTargetId: "method:Foo#run", kind: "method" },
          availableOperations: [{ operation: "rename:method" }],
          diagnostics: [],
        },
      }
    }
    return {
      exitCode: 0,
      stdout: "",
      stderr: "",
      argv,
      json: {
        status: "ok",
        draftId: "draft-1",
        operation: "rename:method",
        resolvedContext: { stableTargetId: "method:Foo#run", kind: "method" },
        inputs: [{ name: "newName", type: "string", required: true }],
        diagnostics: [],
      },
    }
  }
  const preview = await hooks.runIdeaPreviewProtocol({
    worktree: "/p",
    cli: "idea-refactor",
    request: {
      projectRoot: "/p",
      operation: "rename:method",
      target: { fqcn: "demo.Foo", memberName: "run", parameterTypes: [] },
      arguments: { newName: "execute" },
      detail: "compact",
    },
    runner,
  })
  const previewPayload = JSON.parse(preview.output)
  const previewErrors = []
  if (previewPayload.status !== "ready") previewErrors.push(`status=${previewPayload.status}`)
  if (previewPayload.proposalId !== "draft-1") previewErrors.push(`proposalId=${previewPayload.proposalId}`)
  if (previewPayload.nextAction !== "apply") previewErrors.push(`nextAction=${previewPayload.nextAction}`)
  if ("raw" in previewPayload) previewErrors.push("compact preview leaked raw payload")
  if (calls.length !== 2 || calls[0][0] !== "locate" || calls[1][0] !== "prepare") {
    previewErrors.push(`unexpected call sequence=${JSON.stringify(calls.map((argv) => argv[0]))}`)
  }
  if (!calls[1].includes("--draft-id") || !calls[1].includes("draft-1")) {
    previewErrors.push("prepare did not use explicit draft id")
  }
  const initialCalls = calls.map((argv) => argv[0])

  const continuationCalls = []
  await hooks.runIdeaPreviewProtocol({
    worktree: "/p",
    cli: "idea-refactor",
    request: {
      projectRoot: "/p",
      operation: "rename:method",
      proposalId: "draft-1",
      arguments: { newName: "executeAgain" },
    },
    runner: async (worktree, cli, argv) => {
      continuationCalls.push(argv)
      return runner(worktree, cli, argv)
    },
  })
  if (continuationCalls.length !== 1 || continuationCalls[0][0] !== "prepare") {
    previewErrors.push("proposal continuation performed locate")
  }

  const selectionCalls = []
  const selectionPreview = await hooks.runIdeaPreviewProtocol({
    worktree: "/p",
    cli: "idea-refactor",
    request: {
      projectRoot: "/p",
      operation: "extract:method",
      target: { fqcn: "demo.Foo", memberName: "run", parameterTypes: [] },
    },
    runner: async (_worktree, _cli, argv) => {
      selectionCalls.push(argv)
      return {
        exitCode: 0,
        stdout: "",
        stderr: "",
        argv,
        json: {
          status: "ok",
          draftId: "draft-selection",
          availableOperations: [],
          operationCandidates: [{
            operation: "extract:method",
            candidates: [{ startLine: 4, startColumn: 5, endLine: 8, endColumn: 6 }],
          }],
          diagnostics: [],
        },
      }
    },
  })
  const selectionPayload = JSON.parse(selectionPreview.output)
  if (selectionPayload.status !== "needs_selection" || selectionPayload.nextAction !== "preview") {
    previewErrors.push(`selection state=${selectionPayload.status}/${selectionPayload.nextAction}`)
  }
  if (selectionCalls.length !== 1 || selectionCalls[0][0] !== "locate") {
    previewErrors.push("selection discovery unexpectedly prepared")
  }

  const apply = hooks.renderIdeaApplyProtocolResult(
    "draft-1",
    {
      exitCode: 0,
      stdout: "",
      stderr: "",
      argv: ["apply"],
      json: {
        status: "ok",
        applied: true,
        operation: "rename:method",
        result: { changedFiles: 1, changedFilePaths: ["src/Foo.java"] },
        diagnostics: [],
      },
    },
    "compact",
    7,
  )
  const applyPayload = JSON.parse(apply.output)
  if (applyPayload.status !== "applied" || applyPayload.nextAction !== "verify") {
    previewErrors.push(`apply state=${applyPayload.status}/${applyPayload.nextAction}`)
  }
  if (JSON.stringify(applyPayload.changedFilePaths) !== JSON.stringify(["src/Foo.java"])) {
    previewErrors.push(`changedFilePaths=${JSON.stringify(applyPayload.changedFilePaths)}`)
  }
  const stalePayload = JSON.parse(hooks.renderIdeaApplyProtocolResult(
    "draft-1",
    {
      exitCode: 1,
      stdout: "",
      stderr: "",
      argv: ["apply"],
      json: {
        status: "failed",
        applied: false,
        diagnostics: [{ code: "STALE_DRAFT", summary: "Source changed after preview." }],
      },
    },
    "full",
    3,
  ).output)
  if (stalePayload.status !== "stale" || stalePayload.nextAction !== "preview" || !stalePayload.raw) {
    previewErrors.push(`stale state=${stalePayload.status}/${stalePayload.nextAction}`)
  }
  if (previewErrors.length) {
    throw new SelfCheckError("plugin_idea_proposal_assertions", "IDEA proposal protocol self-check failed.", {
      errors: previewErrors,
      preview: previewPayload,
      apply: applyPayload,
    })
  }
  return {
    status: previewPayload.status,
    proposalId: previewPayload.proposalId,
    initialCalls,
    continuationCalls: continuationCalls.map((argv) => argv[0]),
    selectionStatus: selectionPayload.status,
    applyStatus: applyPayload.status,
    staleStatus: stalePayload.status,
    changedFilePaths: applyPayload.changedFilePaths,
  }
}

async function runPluginFailureIntegrationSelfCheck(smellVerify) {
  const failingArgs = {
    projectRoot: `/definitely/not/a/real/path/self-check-${Date.now()}`,
    // Non-Java verification keeps the legacy direct-tool path; Java
    // checkpoint verification is separately required to be controller-owned.
    language: "python",
    smell: "long_method",
    location: "src/Foo.py:1",
    verificationMode: "project_full",
    noSnapshot: true,
  }
  const exitZeroResult = await smellVerify.execute(failingArgs)
  const exitZeroSummary = verifyFailureIntegrationOutput("bridge_failure_exit_zero", exitZeroResult, {
    requireNonZeroExit: false,
  })

  const savedArtifactRoot = process.env.SMELL_ARTIFACT_ROOT
  process.env.SMELL_ARTIFACT_ROOT = "/dev/null/smell-artifacts-self-check"
  let nonZeroResult
  try {
    nonZeroResult = await smellVerify.execute(failingArgs)
  } finally {
    if (savedArtifactRoot === undefined) delete process.env.SMELL_ARTIFACT_ROOT
    else process.env.SMELL_ARTIFACT_ROOT = savedArtifactRoot
  }
  const nonZeroSummary = verifyFailureIntegrationOutput("bridge_non_zero_exit", nonZeroResult, {
    requireNonZeroExit: true,
  })

  return {
    bridgeFailureExitZero: exitZeroSummary,
    bridgeNonZeroExit: nonZeroSummary,
  }
}

function verifyFailureIntegrationOutput(scenarioName, result, options = {}) {
  if (!result || typeof result !== "object" || Array.isArray(result)) {
    throw new SelfCheckError("plugin_failure_integration_shape", `Scenario '${scenarioName}': execute() did not return an object.`, {
      resultType: typeof result,
    })
  }
  if (typeof result.title !== "string") {
    throw new SelfCheckError("plugin_failure_integration_title", `Scenario '${scenarioName}': execute() title is not a string.`, {
      titleType: typeof result.title,
    })
  }
  if (typeof result.output !== "string") {
    throw new SelfCheckError(
      "plugin_failure_integration_output",
      `Scenario '${scenarioName}': execute() output is not a string; OpenCode's text.split would throw.`,
      { outputType: typeof result.output },
    )
  }
  let lines
  try {
    lines = result.output.split("\n")
  } catch (error) {
    throw new SelfCheckError(
      "plugin_failure_integration_split",
      `Scenario '${scenarioName}': execute() output.split threw: ${error instanceof Error ? error.message : String(error)}`,
      {},
    )
  }
  let parsed
  try {
    parsed = JSON.parse(result.output)
  } catch (error) {
    throw new SelfCheckError(
      "plugin_failure_integration_json",
      `Scenario '${scenarioName}': execute() output is not JSON-parseable: ${error instanceof Error ? error.message : String(error)}`,
      { outputPreview: result.output.slice(0, 400) },
    )
  }
  if (typeof parsed.success !== "boolean") {
    throw new SelfCheckError("plugin_failure_integration_success_type", `Scenario '${scenarioName}': parsed.success is not a boolean.`, {
      parsedSuccess: parsed.success,
    })
  }
  if (typeof parsed.status !== "string" || !parsed.status) {
    throw new SelfCheckError("plugin_failure_integration_status", `Scenario '${scenarioName}': parsed.status is missing or not a string.`, {
      parsedStatus: parsed.status,
    })
  }
  if (!parsed.bridge || typeof parsed.bridge.exitCode !== "number") {
    throw new SelfCheckError("plugin_failure_integration_bridge", `Scenario '${scenarioName}': parsed.bridge.exitCode is missing.`, {
      bridge: parsed.bridge,
    })
  }
  if (options.requireNonZeroExit && parsed.bridge.exitCode === 0) {
    throw new SelfCheckError("plugin_failure_integration_exit_code", `Scenario '${scenarioName}': expected non-zero bridge exitCode.`, {
      bridge: parsed.bridge,
    })
  }
  if (!result.metadata || typeof result.metadata !== "object" || Array.isArray(result.metadata)) {
    throw new SelfCheckError("plugin_failure_integration_metadata", `Scenario '${scenarioName}': execute() metadata is not a plain object.`, {
      metadataType: typeof result.metadata,
    })
  }
  if (typeof result.metadata.exitCode !== "number") {
    throw new SelfCheckError("plugin_failure_integration_metadata", `Scenario '${scenarioName}': execute() metadata.exitCode is not a number.`, {
      metadata: result.metadata,
    })
  }
  if (typeof result.metadata.stderr === "string" && result.metadata.stderr.length > 4096) {
    throw new SelfCheckError("plugin_failure_integration_metadata", `Scenario '${scenarioName}': execute() metadata.stderr was not truncated.`, {
      stderrLength: result.metadata.stderr.length,
    })
  }
  try {
    JSON.stringify(result.metadata)
  } catch (error) {
    throw new SelfCheckError("plugin_failure_integration_metadata", `Scenario '${scenarioName}': execute() metadata is not JSON-serializable.`, {
      error: error instanceof Error ? error.message : String(error),
    })
  }
  return {
    outputType: typeof result.output,
    outputLength: result.output.length,
    lineCount: lines.length,
    parsedStatus: parsed.status,
    parsedSuccess: parsed.success,
    bridgeExitCode: parsed.bridge.exitCode,
    metadataExitCode: result.metadata.exitCode,
    metadataStderrLength: typeof result.metadata.stderr === "string" ? result.metadata.stderr.length : null,
  }
}

function makeFailureOutput(status, category, extra = {}) {
  return JSON.stringify({
    success: false,
    status,
    failure_pack: {
      failure_category: category,
      verify_status: status,
      highlights: extra.highlights || ["cannot find symbol Foo"],
      next_action: extra.next_action || "",
      // The Python bridge emits artifact_paths as an OBJECT (name -> path), not
      // a string array. The fixture must mirror the real contract.
      artifact_paths: extra.artifact_paths || { build_log: "/tmp/art/build.log", test_log: "/tmp/art/test.log" },
      recommendations: ["Fix the compile error."],
    },
    bridge: { exitCode: 1, stderr: "" },
  })
}

function makePassOutput() {
  return JSON.stringify({ success: true, status: "PASS", bridge: { exitCode: 0, stderr: "" } })
}

function makeFakeClient() {
  const calls = []
  const client = {
    session: {
      promptAsync(opts) {
        calls.push(opts)
        return Promise.resolve({ data: undefined, error: undefined })
      },
    },
  }
  return { client, calls }
}

function makeFailingFakeClient(errorMessage) {
  const calls = []
  const client = {
    session: {
      promptAsync() {
        calls.push({ failed: true })
        return Promise.resolve({ error: new Error(errorMessage) })
      },
    },
  }
  return { client, calls }
}

const IDLE_AGENT = "java-refactor-agent"
const IDLE_DIR = "/tmp/proj"
const IDLE_TASK = "/tmp/proj|long_method|src/main/java/Foo.java:1"

function assertEqual(scenario, actual, expected, field) {
  if (actual !== expected) {
    throw new SelfCheckError("idle_continue_assertion", `Scenario '${scenario}': ${field} expected ${JSON.stringify(expected)} got ${JSON.stringify(actual)}.`, {
      scenario,
      field,
      actual,
      expected,
    })
  }
}

function assertCond(scenario, cond, message, details = {}) {
  if (!cond) {
    throw new SelfCheckError("idle_continue_assertion", `Scenario '${scenario}': ${message}`, { scenario, ...details })
  }
}

function flush() {
  return new Promise((resolve) => setImmediate(resolve))
}

async function runIdleContinueSelfCheck(pluginModule) {
  const hooks = pluginModule.SmellPlugin?.__selfTest || pluginModule.default?.__selfTest
  for (const key of [
    "classifyFailureForContinue",
    "makeTaskKey",
    "buildContinuationMessage",
    "buildVerifyRequiredMessage",
    "createIdleContinueRuntime",
    "SMELL_IDLE_CONTINUE_PREFIX",
  ]) {
    assertCond(`unified_loop_hook:${key}`, Boolean(hooks && key in hooks), `missing ${key}`)
  }
  for (const removed of [
    "idleContinueMode",
    "isOpendcodeRunMode",
    "isBatchEnvironment",
    "MAX_IDLE_CONTINUE_ATTEMPTS",
  ]) {
    assertCond(`unified_loop_removed:${removed}`, !(removed in hooks), `${removed} must be removed`)
  }

  const removedStructuralFailure = hooks.classifyFailureForContinue({
    failure_category: "STRUCTURAL_ROUTE_MISMATCH",
    verify_status: "SMELL_GUARD_FAILED",
    highlights: ["CAPABILITY_SPLIT_REQUIRED target=ReadOnlyPacket method=toBytes parent=Packet"],
    artifact_paths: {},
  })
  assertEqual("structural_route_category_removed", removedStructuralFailure.ok, false, "ok")

  function outputWithLoop({ decision = "continue", continuation = 1, max = 2, status = "SMELL_GUARD_FAILED", nextAction = "" } = {}) {
    const payload = JSON.parse(makeFailureOutput(status, status, { next_action: nextAction }))
    payload.loop = {
      decision,
      continuation,
      max_continuations: max,
      instruction: decision === "continue" ? "repair from the latest evidence" : "",
    }
    return JSON.stringify(payload)
  }

  function record(rt, options = {}) {
    return rt.recordFromBridgeOutput({
      sessionID: options.sessionID || "s1",
      agent: options.agent || "any-refactor-agent",
      directory: options.directory || IDLE_DIR,
      taskKey: options.taskKey || IDLE_TASK,
      output: options.output || outputWithLoop(options),
    })
  }

  // All OpenCode modes and batch-like environments use exactly the same path.
  for (const modeCase of [
    { name: "tui", argv: ["opencode"] },
    { name: "run", argv: ["opencode", "run"] },
    { name: "serve", argv: ["opencode", "serve"] },
    { name: "web", argv: ["opencode", "web"] },
    { name: "attach", argv: ["opencode", "attach"] },
    { name: "batch", argv: ["opencode", "run"], env: { SMELL_BATCH_RUN: "1", SMELL_PROJECT_ROOT: "/tmp/project" } },
  ]) {
    const { client, calls } = makeFakeClient()
    const rt = hooks.createIdleContinueRuntime({ client, argv: modeCase.argv, env: modeCase.env || {} })
    const metadata = record(rt)
    assertEqual(`unified_${modeCase.name}_enabled`, metadata.enabled, true, "enabled")
    assertEqual(`unified_${modeCase.name}_continuation`, metadata.continuation, 1, "continuation")
    assertEqual(`unified_${modeCase.name}_max`, metadata.maxContinuations, 2, "maxContinuations")
    assertEqual(`unified_${modeCase.name}_dispatch`, rt.handleIdle("s1"), true, "dispatch")
    await flush()
    assertEqual(`unified_${modeCase.name}_calls`, calls.length, 1, "calls")
  }

  // The loop decision owns the only budget. Dispatch does not increment it.
  {
    const { client, calls } = makeFakeClient()
    const rt = hooks.createIdleContinueRuntime({ client })
    record(rt, { continuation: 1, max: 2 })
    rt.handleIdle("s1")
    await flush()
    assertEqual("unified_budget_round1", calls.length, 1, "calls")
    assertEqual("unified_budget_value1", rt.peek("s1").continuation, 1, "continuation")

    record(rt, { continuation: 2, max: 2 })
    rt.handleIdle("s1")
    await flush()
    assertEqual("unified_budget_round2", calls.length, 2, "calls")
    assertEqual("unified_budget_value2", rt.peek("s1").continuation, 2, "continuation")

    // The plugin may authorize one cap recovery while keeping the public
    // continuation value at max. A fresh generation must still dispatch in
    // every OpenCode surface.
    record(rt, { continuation: 2, max: 2 })
    rt.handleIdle("s1")
    await flush()
    assertEqual("unified_cap_recovery_round", calls.length, 3, "calls")
    assertEqual("unified_cap_recovery_value", rt.peek("s1").continuation, 2, "continuation")

    record(rt, { decision: "stop", continuation: 2, max: 2 })
    assertEqual("unified_budget_stop_dispatch", rt.handleIdle("s1"), false, "dispatch")
    await flush()
    assertEqual("unified_budget_stop_calls", calls.length, 3, "calls")
  }

  // The source-derived detector plan is rendered before compact highlights;
  // it is not lost behind the three-highlight continuation cap.
  {
    const { client, calls } = makeFakeClient()
    const rt = hooks.createIdleContinueRuntime({ client })
    const required = "extract cohesive blocks totaling at least 15 AST-NCSS from the frozen method"
    record(rt, { sessionID: "precise-next", nextAction: required })
    rt.handleIdle("precise-next")
    await flush()
    assertCond(
      "unified_precise_next_action",
      calls[0].body.parts[0].text.includes(`Required next action: ${required}`),
      "source-derived next action missing from continuation prompt",
    )
  }

  // A generation dispatches once; PASS or malformed output revokes pending.
  {
    const { client, calls } = makeFakeClient()
    const rt = hooks.createIdleContinueRuntime({ client })
    record(rt)
    rt.handleIdle("s1")
    rt.handleIdle("s1")
    await flush()
    assertEqual("unified_generation_once", calls.length, 1, "calls")

    record(rt, { output: makePassOutput() })
    assertEqual("unified_pass_stops", rt.handleIdle("s1"), false, "dispatch")
    record(rt)
    record(rt, { output: "not-json" })
    assertEqual("unified_malformed_stops", rt.handleIdle("s1"), false, "dispatch")
  }

  // A task that idles before its first verify receives exactly one reminder.
  {
    const { client, calls } = makeFakeClient()
    const rt = hooks.createIdleContinueRuntime({ client })
    rt.armInitialVerification({
      sessionID: "initial",
      agent: "java-refactor-agent",
      directory: IDLE_DIR,
      maxContinuations: 2,
      instruction: "repair narrowly",
    })
    assertEqual("verify_required_initial_dispatch", rt.handleIdle("initial"), true, "dispatch")
    await flush()
    assertEqual("verify_required_initial_calls", calls.length, 1, "calls")
    assertCond(
      "verify_required_initial_text",
      calls[0].body.parts[0].text.includes("No smell_verify call has completed"),
      "initial reminder text missing",
    )
    assertEqual("verify_required_initial_once", rt.handleIdle("initial"), false, "dispatch")
  }

  // A dispatched corrective continuation must close with another verify. The
  // reminder is one-shot and a real verify result clears it.
  {
    const { client, calls } = makeFakeClient()
    const rt = hooks.createIdleContinueRuntime({ client })
    record(rt, { sessionID: "after-continuation" })
    assertEqual("verify_required_continuation_dispatch", rt.handleIdle("after-continuation"), true, "dispatch")
    await flush()
    assertEqual("verify_required_continuation_prompt", calls.length, 1, "calls")
    assertEqual("verify_required_after_continuation", rt.handleIdle("after-continuation"), true, "dispatch")
    await flush()
    assertEqual("verify_required_after_continuation_calls", calls.length, 2, "calls")
    assertCond(
      "verify_required_after_continuation_text",
      calls[1].body.parts[0].text.includes("corrective continuation ended without a new smell_verify"),
      "continuation reminder text missing",
    )
    assertEqual("verify_required_after_continuation_once", rt.handleIdle("after-continuation"), false, "dispatch")
    record(rt, { sessionID: "after-continuation", output: makePassOutput() })
    assertEqual("verify_required_cleared_by_verify", rt.handleIdle("after-continuation"), false, "dispatch")
  }

  return {
    modes: ["tui", "run", "serve", "web", "attach", "batch"],
    sharedBudget: true,
    verifyClosure: true,
    passed: true,
  }
}

function runCommandPolicyDecisionSelfCheck(pluginModule) {
  const hooks = pluginModule.SmellPlugin?.__selfTest || pluginModule.default?.__selfTest
  assertCond("command_decision_hook", typeof hooks?.applyCommandLoopDecision === "function", "missing applyCommandLoopDecision")
  assertCond("command_state_snapshot_hook", typeof hooks?.commandLoopStateSnapshot === "function", "missing commandLoopStateSnapshot")
  assertCond("command_state_restore_hook", typeof hooks?.restoreCommandLoopState === "function", "missing restoreCommandLoopState")
  assertEqual(
    "java_checkpoint_identity_detected",
    hooks.isJavaCheckpointIdentity({ language: "java", smell: "long_method", location: "Foo.java:1" }),
    true,
    "identity",
  )
  assertEqual(
    "non_java_identity_unchanged",
    hooks.isJavaCheckpointIdentity({ language: "python", smell: "long_method", location: "foo.py:1" }),
    false,
    "identity",
  )
  const state = {
    policy: {
      task: "task",
      verification_mode: "project_full",
      allow_test_changes: false,
      loop: {
        mode: "verify-failure",
        max_continuations: 2,
        no_progress_limit: 1,
        allowed_failure_groups: ["smell"],
        instruction: "repair narrowly",
        sample_deadline_seconds: 1800,
      },
    },
    startedAt: Date.now(),
    continuationCount: 0,
    capRecoveryUsed: false,
    noProgressCount: 0,
    lastFailureFingerprint: "",
  }
  const failure = {
    success: false,
    status: "SMELL_GUARD_FAILED",
    failure_pack: {
      failure_category: "SMELL_GUARD_FAILED",
      failure_group: "smell",
      retryable: true,
      verify_status: "SMELL_GUARD_FAILED",
      highlights: ["still too long"],
    },
  }
  const first = { output: JSON.stringify(failure), metadata: {} }
  hooks.applyCommandLoopDecision(first, state)
  const firstPayload = JSON.parse(first.output)
  assertEqual("command_decision_continue", firstPayload.loop.decision, "continue", "decision")
  assertEqual("command_decision_count", firstPayload.loop.continuation, 1, "continuation")
  assertEqual("command_decision_instruction", firstPayload.loop.instruction, "repair narrowly", "instruction")
  const routeLockedPrompt = hooks.commandPolicyPrompt({
    ...state.policy,
    task: [
      "Project root: /tmp/project",
      "Smell type: refused_bequest",
      "Target location: Child.java:method=toBytes|line=10",
      "Smell evidence: parents=Packet; structural_expectation=capability_split; refactor_path=split_read_from_write",
    ].join("\n"),
  })
  assertCond(
    "command_prompt_ignores_dataset_route_lock",
    !routeLockedPrompt.includes("Mandatory Refused Bequest route lock:")
      && routeLockedPrompt.includes("Baseline capture must uniquely confirm the requested smell at the supplied target")
      && routeLockedPrompt.includes("context selects the entity but never supplies a verdict"),
    "dataset route metadata entered the command contract",
  )
  assertCond(
    "checkpoint_target_identity_prompt_hook",
    typeof hooks?.checkpointTargetIdentityPrompt === "function",
    "missing checkpointTargetIdentityPrompt",
  )
  const featureTargetIdentityPrompt = hooks.checkpointTargetIdentityPrompt("feature_envy", {
    guard_contract: {
      target_id: "feature-target",
      entity_identity: { method: "render()", field: "document", envied_type: "example.Document" },
    },
    resolution_plan: {
      route_family: "close-one-receiver-collaboration",
      next_action: "close the complete document receiver collaboration",
      worklist: [{ kind: "receiver_cluster", field: "document", envied_type: "example.Document" }],
      forbidden: ["move the finding to another method in the same source owner"],
    },
    metrics: {
      method: "render()",
      envied_field: "document",
      envied_type: "example.Document",
      objectives: { envy_access_count: 9, envy_access_diff: 7 },
    },
  })
  assertCond(
    "checkpoint_target_identity_exact_receiver",
    featureTargetIdentityPrompt.includes("field=document")
      && featureTargetIdentityPrompt.includes("envied_type=example.Document")
      && featureTargetIdentityPrompt.includes("close-one-receiver-collaboration"),
    "feature-envy Guard identity missing from prompt",
  )
  assertCond(
    "checkpoint_target_identity_has_no_metric_coaching",
    !featureTargetIdentityPrompt.includes("envy_access_count")
      && !featureTargetIdentityPrompt.includes("envy_access_diff")
      && !featureTargetIdentityPrompt.includes("threshold")
      && !featureTargetIdentityPrompt.includes("PASS target"),
    "feature-envy identity prompt leaked metric coaching",
  )
  const dataClumpsContractPrompt = hooks.checkpointTargetIdentityPrompt("data_clumps", {
    guard_contract: {
      target_id: "clump-target",
      entity_identity: { group: "int:x|string:y|boolean:z" },
    },
    resolution_plan: {
      route_family: "migrate-semantic-occurrence-component",
      next_action: "migrate the complete typed parameter group and remove every old-group wrapper",
      worklist: [{ kind: "remaining_occurrence", file: "src/Tile.java", method: "setTile" }],
      forbidden: ["retain the old parameter group in a wrapper"],
    },
    metrics: {
      finding_identity: { group: "int:x|string:y|boolean:z" },
    },
  })
  assertCond(
    "checkpoint_target_identity_data_clumps_product_contract",
    dataClumpsContractPrompt.includes("int:x|string:y|boolean:z")
      && dataClumpsContractPrompt.includes("migrate-semantic-occurrence-component")
      && dataClumpsContractPrompt.includes("remove every old-group wrapper"),
    "data-clumps product contract missing from prompt",
  )
  assertEqual(
    "plugin_has_no_posthoc_test_gate",
    typeof hooks?.applyImmutableTestSourceGate,
    "undefined",
    "posthoc test gate",
  )
  const restoredAfterRestart = hooks.restoreCommandLoopState(
    JSON.stringify(hooks.commandLoopStateSnapshot(state)),
  )
  assertCond("command_state_restored", Boolean(restoredAfterRestart), "state did not restore")
  assertEqual("command_state_count_survives_restart", restoredAfterRestart.continuationCount, 1, "continuationCount")
  assertEqual(
    "command_state_fingerprint_survives_restart",
    restoredAfterRestart.lastFailureFingerprint,
    state.lastFailureFingerprint,
    "lastFailureFingerprint",
  )
  const restartedSecond = { output: JSON.stringify(failure), metadata: {} }
  hooks.applyCommandLoopDecision(restartedSecond, restoredAfterRestart)
  assertEqual(
    "command_state_no_progress_survives_restart",
    JSON.parse(restartedSecond.output).loop.termination_reason,
    "NO_PROGRESS",
    "termination",
  )
  const second = { output: JSON.stringify(failure), metadata: {} }
  hooks.applyCommandLoopDecision(second, state)
  const secondPayload = JSON.parse(second.output)
  assertEqual("command_decision_no_progress", secondPayload.loop.termination_reason, "NO_PROGRESS", "termination")

  // An IMPROVED result keeps the loop running toward
  // resolved (with the bridge continue_hint), and only identical best-partial
  // objectives across verifies count as no-progress.
  const improvedState = {
    policy: state.policy,
    startedAt: Date.now(),
    continuationCount: 0,
    capRecoveryUsed: false,
    noProgressCount: 0,
    lastFailureFingerprint: "",
  }
  const improved = {
    success: false,
    accepted: false,
    progress: true,
    status: "IMPROVED",
    resolution: "improved",
    continue_hint: "keep going to resolved",
    failure_pack: {
      failure_category: "SMELL_GUARD_FAILED",
      failure_group: "smell",
      retryable: true,
    },
    checkpoint: { best_partial: { objectives: { loc: 400 } } },
  }
  const improvedFirst = { output: JSON.stringify(improved), metadata: {} }
  hooks.applyCommandLoopDecision(improvedFirst, improvedState)
  const improvedFirstPayload = JSON.parse(improvedFirst.output)
  assertEqual("improved_decision_continue", improvedFirstPayload.loop.decision, "continue", "decision")
  assertEqual("improved_instruction_hint", improvedFirstPayload.loop.instruction, "keep going to resolved", "instruction")
  const improvedSecond = { output: JSON.stringify(improved), metadata: {} }
  hooks.applyCommandLoopDecision(improvedSecond, improvedState)
  const improvedSecondPayload = JSON.parse(improvedSecond.output)
  assertEqual(
    "improved_no_progress_stop",
    improvedSecondPayload.loop.termination_reason,
    "IMPROVED_NO_PROGRESS",
    "termination",
  )
  assertEqual("improved_no_progress_decision", improvedSecondPayload.loop.decision, "stop", "decision")

  const resolved = { success: true, accepted: true, status: "PASS", resolution: "resolved" }
  const resolvedResult = { output: JSON.stringify(resolved), metadata: {} }
  hooks.applyCommandLoopDecision(resolvedResult, improvedState)
  const resolvedPayload = JSON.parse(resolvedResult.output)
  assertEqual("resolved_decision_stop", resolvedPayload.loop.decision, "stop", "decision")
  assertEqual("resolved_termination", resolvedPayload.loop.termination_reason, "PASS", "termination")
  for (const [name, inconsistent] of [
    ["missing_accepted", { success: true, status: "PASS", resolution: "resolved" }],
    ["missing_resolution", { success: true, accepted: true, status: "PASS" }],
    ["false_success", { success: false, accepted: true, status: "PASS", resolution: "resolved" }],
    ["legacy_success_only", { success: true }],
  ]) {
    const inconsistentState = {
      ...improvedState,
      continuationCount: 0,
      noProgressCount: 0,
      lastFailureFingerprint: "",
    }
    const result = { output: JSON.stringify(inconsistent), metadata: {} }
    hooks.applyCommandLoopDecision(result, inconsistentState)
    const payload = JSON.parse(result.output)
    assertEqual(`inconsistent_pass_${name}`, payload.loop.termination_reason, "NON_REPAIRABLE_FAILURE", "termination")
  }

  const capPolicy = {
    ...state.policy,
    loop: {
      ...state.policy.loop,
      no_progress_limit: 3,
      allowed_failure_groups: ["smell", "compile", "test"],
    },
  }
  const capState = {
    policy: capPolicy,
    startedAt: Date.now(),
    continuationCount: 2,
    capRecoveryUsed: false,
    noProgressCount: 0,
    lastFailureFingerprint: "",
  }
  const progressingAtCap = {
    ...failure,
    checkpoint: { delta: { metric_progress: true } },
  }
  const capRecovery = { output: JSON.stringify(progressingAtCap), metadata: {} }
  hooks.applyCommandLoopDecision(capRecovery, capState)
  const capRecoveryPayload = JSON.parse(capRecovery.output)
  assertEqual("cap_progress_recovers_once", capRecoveryPayload.loop.decision, "continue", "decision")
  assertEqual("cap_recovery_count_bounded", capRecoveryPayload.loop.continuation, 2, "continuation")
  assertEqual("cap_recovery_marked", capRecoveryPayload.loop.cap_recovery_used, true, "cap_recovery_used")
  const restoredCapState = hooks.restoreCommandLoopState(
    JSON.stringify(hooks.commandLoopStateSnapshot(capState)),
  )
  const restartedCapRecovery = { output: JSON.stringify(progressingAtCap), metadata: {} }
  hooks.applyCommandLoopDecision(restartedCapRecovery, restoredCapState)
  assertEqual(
    "cap_recovery_survives_restart",
    JSON.parse(restartedCapRecovery.output).loop.termination_reason,
    "MAX_CONTINUATIONS_REACHED",
    "termination",
  )
  const capRecoveryAgain = { output: JSON.stringify(progressingAtCap), metadata: {} }
  hooks.applyCommandLoopDecision(capRecoveryAgain, capState)
  const capRecoveryAgainPayload = JSON.parse(capRecoveryAgain.output)
  assertEqual("cap_recovery_only_once", capRecoveryAgainPayload.loop.decision, "stop", "decision")
  assertEqual("cap_recovery_then_max", capRecoveryAgainPayload.loop.termination_reason, "MAX_CONTINUATIONS_REACHED", "termination")

  const compileCapState = {
    ...capState,
    capRecoveryUsed: false,
    noProgressCount: 0,
    lastFailureFingerprint: "",
  }
  const compileAtCap = {
    success: false,
    status: "BUILD_FAILED",
    failure_pack: {
      failure_category: "BUILD_FAILED",
      failure_group: "compile",
      retryable: true,
      verify_status: "BUILD_FAILED",
      highlights: ["compile repair remains"],
    },
    checkpoint: { delta: { has_production_diff: true } },
  }
  const compileRecovery = { output: JSON.stringify(compileAtCap), metadata: {} }
  hooks.applyCommandLoopDecision(compileRecovery, compileCapState)
  assertEqual("compile_checkpoint_diff_recovers_at_cap", JSON.parse(compileRecovery.output).loop.decision, "continue", "decision")

  const noProgressCapState = {
    ...capState,
    capRecoveryUsed: false,
    noProgressCount: 0,
    lastFailureFingerprint: "",
  }
  const noProgressAtCap = {
    ...failure,
    checkpoint: { delta: { metric_progress: false } },
  }
  const noCapRecovery = { output: JSON.stringify(noProgressAtCap), metadata: {} }
  hooks.applyCommandLoopDecision(noCapRecovery, noProgressCapState)
  assertEqual("cap_without_progress_stops", JSON.parse(noCapRecovery.output).loop.termination_reason, "MAX_CONTINUATIONS_REACHED", "termination")
  return { passed: true }
}

async function runPluginSelfCheck(fixtureRoot, artifactRoot) {
  const tempRoot = await mkdtemp(path.join(os.tmpdir(), "smell-verify-self-check-plugin-"))
  try {
    const compiledFile = await compilePluginForSelfCheck(tempRoot)
    const moduleUrl = `${pathToFileURL(compiledFile).href}?self_check=${Date.now()}`
    const pluginModule = await import(moduleUrl)
    if (typeof pluginModule.SmellPlugin !== "function") {
      throw new SelfCheckError("plugin_load", "Compiled plugin does not export SmellPlugin.", {
        exports: Object.keys(pluginModule),
      })
    }

    const envBefore = { ...process.env }
    Object.assign(process.env, cleanSmellIdentityEnv(process.env), {
      SMELL_ARTIFACT_ROOT: artifactRoot,
      SMELL_PROJECTS: path.join(fixtureRoot, "projects.yaml"),
    })
    try {
      const plugin = await pluginModule.SmellPlugin({ worktree: fixtureRoot })
      const smellVerify = plugin?.tool?.smell_verify
      if (!smellVerify || typeof smellVerify.execute !== "function") {
        throw new SelfCheckError("tool_lookup", "smell_verify tool was not registered with an execute function.", {
          pluginKeys: plugin && typeof plugin === "object" ? Object.keys(plugin) : [],
          toolKeys: plugin?.tool && typeof plugin.tool === "object" ? Object.keys(plugin.tool) : [],
          smellVerifyKeys: smellVerify && typeof smellVerify === "object" ? Object.keys(smellVerify) : [],
        })
      }
      const commandHook = plugin?.["command.execute.before"]
      if (typeof commandHook !== "function") {
        throw new SelfCheckError("command_policy_hook", "command.execute.before hook was not registered.", {})
      }
      try {
        await smellVerify.execute(
          {
            projectRoot: fixtureRoot,
            language: "java",
            smell: "long_method",
            location: "src/main/java/SelfCheckSample.java:2",
            verificationMode: "project_full",
          },
          { sessionID: "unowned-java-checkpoint", agent: "java-refactor-agent", directory: fixtureRoot },
        )
        throw new SelfCheckError("unowned_checkpoint", "Unowned Java checkpoint verification was not rejected.", {})
      } catch (error) {
        assertCond(
          "unowned_java_checkpoint_fails_closed",
          String(error?.message || error).includes("CHECKPOINT_CONTROLLER_IDENTITY_MISSING"),
          String(error?.message || error),
        )
      }
      const commandOutput = { parts: [{ type: "text", text: "placeholder" }] }
      await commandHook(
        {
          command: "java-refactor-run",
          sessionID: "command-policy-self-check",
          arguments: `--verification-mode=project_full --loop-max=2 --loop-no-progress-limit=1 -- Project root: ${fixtureRoot}\nSmell type: long_method\nTarget location: src/main/java/SelfCheckSample.java:2`,
        },
        commandOutput,
      )
      assertCond(
        "command_policy_prompt",
        String(commandOutput.parts?.[0]?.text || "").includes("Controller-owned verification and loop policy"),
        "command hook must inject canonical policy",
      )
      const sealProbe = await run(
        "python3",
        [
          bridgeFile,
          "capture-baseline",
          "--project-root", fixtureRoot,
          "--language", "java",
          "--smell", "long_method",
          "--location", "src/main/java/SelfCheckSample.java:2",
          "--projects", path.join(fixtureRoot, "projects.yaml"),
          "--verification-mode", "project_full",
        ],
        { cwd: fixtureRoot, env: process.env },
      )
      assertEqual("controller_seal_probe_rc", sealProbe.exitCode, 0, "exitCode")
      const controllerSeal = String(parseJson("controller_seal_probe", sealProbe.stdout).baseline_seal || "")
      assertCond("controller_seal_probe_value", controllerSeal.length > 0, "baseline seal missing")
      const untrustedConfig = path.join(tempRoot, "model-refactor.yaml")
      const untrustedProjects = path.join(tempRoot, "model-projects.yaml")
      await writeFile(
        untrustedConfig,
        "defaults:\n  run_build: false\n  run_tests: false\nlanguages:\n  java:\n    smells:\n      mysterious_name:\n        guards: []\n",
        "utf8",
      )
      await writeFile(
        untrustedProjects,
        `projects:\n- root: ${JSON.stringify(tempRoot)}\n  language: java\n  build:\n    command: \"true\"\n  test:\n    command: \"true\"\n`,
        "utf8",
      )
      const verifyArgs = {
        // These model-controlled values must not replace the identity and
        // configuration frozen by command.execute.before.
        projectRoot: tempRoot,
        language: "java",
        smell: "mysterious_name",
        location: "Missing.java:1",
        targetContextJson: '{"symbol_kind":"local","symbol_name":"forged"}',
        verificationMode: "sample_optimized",
        config: untrustedConfig,
        projects: untrustedProjects,
        noSnapshot: true,
      }
      const verifyContext = {
        sessionID: "command-policy-self-check",
        agent: "java-refactor-agent",
        directory: fixtureRoot,
      }
      const unchangedResult = await smellVerify.execute(verifyArgs, verifyContext)
      const unchangedPayload = parseJson("checkpoint_unchanged_tool_result", unchangedResult.output)
      assertEqual("checkpoint_unchanged_status", unchangedPayload.status, "SMELL_GUARD_FAILED", "status")
      assertEqual("checkpoint_unchanged_loop", unchangedPayload.loop?.decision, "continue", "loop.decision")
      assertEqual(
        "checkpoint_unchanged_reason",
        unchangedPayload.smell_guard?.results?.[0]?.details?.reason,
        "EDIT_REQUIRED",
        "checkpoint reason",
      )
      await writeFile(
        path.join(fixtureRoot, "src", "main", "java", "SelfCheckSample.java"),
        [
          "public class SelfCheckSample {",
          "  public void add(int left, int right) {}",
          "}",
          "",
        ].join("\n"),
        "utf8",
      )
      const repairedResult = await smellVerify.execute(verifyArgs, verifyContext)
      const successPath = normalizeToolResult(repairedResult)
      assertEqual("command_policy_pass_decision", successPath.loop?.decision, "stop", "loop.decision")
      assertEqual("command_policy_pass_reason", successPath.loop?.termination_reason, "PASS", "termination_reason")

      Object.assign(process.env, {
        SMELL_PROJECT_ROOT: fixtureRoot,
        SMELL_LANGUAGE: "java",
        SMELL_SMELL: "long_method",
        SMELL_LOCATION: "src/main/java/SelfCheckSample.java:2",
        SMELL_VERIFICATION_MODE: "project_full",
      })
      delete process.env.SMELL_BASELINE_SEAL
      const reloadedWithoutSeal = await pluginModule.SmellPlugin({ worktree: fixtureRoot })
      try {
        await reloadedWithoutSeal.tool.smell_verify.execute(verifyArgs, {
          sessionID: "batch-reload-no-seal",
          agent: "java-refactor-agent",
          directory: fixtureRoot,
        })
        throw new SelfCheckError("batch_reload_no_seal", "Batch identity without its external seal was accepted.", {})
      } catch (error) {
        assertCond(
          "batch_reload_without_seal_fails_closed",
          String(error?.message || error).includes("CHECKPOINT_CONTROLLER_SEAL_MISSING"),
          String(error?.message || error),
        )
      }
      process.env.SMELL_BASELINE_SEAL = controllerSeal
      const reloadedWithSeal = await pluginModule.SmellPlugin({ worktree: fixtureRoot })
      const reloadedResult = await reloadedWithSeal.tool.smell_verify.execute(verifyArgs, {
        sessionID: "batch-reload-with-seal",
        agent: "java-refactor-agent",
        directory: fixtureRoot,
      })
      const reloadedPayload = parseJson("batch_reload_with_seal", reloadedResult.output)
      assertEqual("batch_reload_external_seal_status", reloadedPayload.status, "PASS", "status")
      assertEqual("batch_reload_external_seal_resolution", reloadedPayload.resolution, "resolved", "resolution")
      assertEqual("batch_reload_external_seal_accepted", reloadedPayload.accepted, true, "accepted")
      for (const key of [
        "SMELL_PROJECT_ROOT",
        "SMELL_LANGUAGE",
        "SMELL_SMELL",
        "SMELL_LOCATION",
        "SMELL_VERIFICATION_MODE",
        "SMELL_BASELINE_SEAL",
      ]) delete process.env[key]
      const normalizeUnit = await runPluginNormalizeSelfCheck(pluginModule)
      const failureIntegration = await runPluginFailureIntegrationSelfCheck(smellVerify)
      const idleContinue = await runIdleContinueSelfCheck(pluginModule)
      const commandPolicyDecision = runCommandPolicyDecisionSelfCheck(pluginModule)
      return {
        successPath,
        unchangedCheckpoint: {
          status: unchangedPayload.status,
          reason: unchangedPayload.smell_guard?.results?.[0]?.details?.reason,
          loop: unchangedPayload.loop,
        },
        normalizeUnit,
        failureIntegration,
        idleContinue,
        commandPolicy: { passed: true },
        commandPolicyDecision,
      }
    } finally {
      for (const key of Object.keys(process.env)) {
        if (!(key in envBefore)) delete process.env[key]
      }
      Object.assign(process.env, envBefore)
    }
  } finally {
    await rm(tempRoot, { recursive: true, force: true })
  }
}

async function main() {
  const options = parseArgs(process.argv.slice(2))
  if (options.ideaProtocolOnly) {
    const tempRoot = await mkdtemp(path.join(os.tmpdir(), "idea-proposal-self-check-"))
    try {
      const compiledFile = await compilePluginForSelfCheck(tempRoot)
      const pluginModule = await import(`${pathToFileURL(compiledFile).href}?idea_protocol=${Date.now()}`)
      const result = await runPluginNormalizeSelfCheck(pluginModule)
      console.log(JSON.stringify({
        success: true,
        node: process.version,
        ideaProposalProtocol: result.ideaProposalResults,
      }, null, 2))
      return
    } finally {
      await rm(tempRoot, { recursive: true, force: true })
    }
  }
  const fixtureRoot = await makeFixtureProject()
  const artifactRoot = await mkdtemp(path.join(os.tmpdir(), "smell-verify-self-check-artifacts-"))
  try {
    const bridge = await runBridgeSelfCheck(fixtureRoot, artifactRoot)
    const pluginSelfCheck = await runPluginSelfCheck(fixtureRoot, artifactRoot)
    const datasetSmoke = options.requireDataset ? await runDatasetSmokeSelfCheck(options) : null
    const report = {
      success: true,
      node: process.version,
      root,
      dependencies: {
        rootPlugin: await readPackageVersion(root, "@opencode-ai/plugin"),
        rootSdk: await readPackageVersion(root, "@opencode-ai/sdk"),
        opencodePlugin: await readPackageVersion(path.join(root, ".opencode"), "@opencode-ai/plugin"),
        opencodeSdk: await readPackageVersion(path.join(root, ".opencode"), "@opencode-ai/sdk"),
      },
      bridge,
      smellVerifyTool: pluginSelfCheck.successPath,
      unchangedCheckpoint: pluginSelfCheck.unchangedCheckpoint,
      smellVerifyFailurePaths: {
        normalizeUnit: pluginSelfCheck.normalizeUnit,
        integration: pluginSelfCheck.failureIntegration,
      },
      simulatedOpenCodeConsumer: {
        splitAccepted: true,
        jsonAccepted: true,
        coversBridgeNonJson: true,
        coversBridgeNonZeroExit: true,
        coversIdeaResultShape: true,
      },
      idleContinue: pluginSelfCheck.idleContinue,
    }
    if (datasetSmoke) {
      report.datasetSmoke = datasetSmoke
    }
    console.log(JSON.stringify(report, null, 2))
  } finally {
    await rm(fixtureRoot, { recursive: true, force: true })
    await rm(artifactRoot, { recursive: true, force: true })
  }
}

main().catch((error) => {
  const payload = {
    success: false,
    stage: error instanceof SelfCheckError ? error.stage : "unexpected",
    message: error instanceof Error ? error.message : String(error),
    details: error instanceof SelfCheckError ? error.details : {},
    node: process.version,
  }
  console.error(JSON.stringify(payload, null, 2))
  process.exit(1)
})

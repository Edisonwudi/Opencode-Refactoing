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
    dataset: process.env.SELF_CHECK_DATASET || "/opt/dataset/java/delivery_schema/mysterious_name.csv",
    sampleId: process.env.SELF_CHECK_SAMPLE_ID || "8",
    model: process.env.SELF_CHECK_MODEL || "zai/glm-4.7",
  }
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index]
    if (arg === "--require-dataset") {
      options.requireDataset = true
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
        "local",
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
      "    return left + right;",
      "  }",
      "}",
      "",
    ].join("\n"),
    "utf8",
  )
  return fixtureRoot
}

async function runBridgeSelfCheck(fixtureRoot, artifactRoot) {
  const env = cleanSmellIdentityEnv(process.env)
  env.SMELL_ARTIFACT_ROOT = artifactRoot
  const result = await run(
    "python3",
    [
      bridgeFile,
      "verify",
      "--project-root",
      fixtureRoot,
      "--language",
      "java",
      "--smell",
      "long_method",
      "--location",
      "src/main/java/SelfCheckSample.java:2",
      "--verification-mode",
      "local",
      "--no-snapshot",
    ],
    { cwd: fixtureRoot, env },
  )
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
  return { hookKeys: Object.keys(hooks).sort(), maxLen, scenarios: results, ideaResults }
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
      wrapperExitCode: parsed.wrapper ? parsed.wrapper.exit_code : null,
      metadataExitCode: typeof rendered.metadata.exitCode === "number" ? rendered.metadata.exitCode : null,
      metadataStderrLength: typeof rendered.metadata.stderr === "string" ? rendered.metadata.stderr.length : null,
    })
  }
  return results
}

async function runPluginFailureIntegrationSelfCheck(smellVerify) {
  const failingArgs = {
    projectRoot: `/definitely/not/a/real/path/self-check-${Date.now()}`,
    language: "java",
    smell: "long_method",
    location: "src/main/java/Foo.java:1",
    verificationMode: "local",
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

async function runIsOpendcodeRunModeSelfCheck(hooks) {
  const cases = [
    // run / serve / web / attach are non-interactive and must be detected.
    { name: "node_then_opencode_run", argv: ["node", "/usr/local/bin/opencode", "run", "task"], expected: true },
    { name: "opencode_run", argv: ["/usr/local/bin/opencode", "run", "task"], expected: true },
    { name: "opencode_exe_run", argv: ["opencode.exe", "run", "task"], expected: true },
    { name: "opencode_run_only", argv: ["/usr/local/bin/opencode", "run"], expected: true },
    { name: "opencode_serve", argv: ["opencode", "serve"], expected: true },
    { name: "opencode_web", argv: ["opencode", "web"], expected: true },
    { name: "opencode_attach", argv: ["opencode", "attach"], expected: true },
    // Bare `opencode` is the interactive TUI (the README's enable command).
    { name: "opencode_no_subcommand", argv: ["/usr/local/bin/opencode"], expected: false },
    { name: "opencode_tui_subcommand", argv: ["/usr/local/bin/opencode", "tui"], expected: false },
    { name: "opencode_with_env_prefix", argv: ["env", "SMELL_IDLE_CONTINUE_MODE=interactive", "opencode"], expected: false },
    // Unrecognizable argv is conservatively treated as run.
    { name: "empty_argv", argv: [], expected: true },
    { name: "non_array_argv", argv: null, expected: true },
    { name: "no_opencode_executable", argv: ["node", "script.js"], expected: true },
    // `run` not immediately after opencode is not a run invocation.
    { name: "run_in_wrong_position", argv: ["opencode", "tui", "run"], expected: false },
  ]
  const results = []
  for (const c of cases) {
    const actual = hooks.isOpendcodeRunMode(c.argv)
    assertEqual(`runmode:${c.name}`, actual, c.expected, "isOpendcodeRunMode")
    results.push({ name: c.name, expected: c.expected, actual })
  }
  return results
}

async function runIdleContinueSelfCheck(pluginModule) {
  const hooks = pluginModule.SmellPlugin?.__selfTest || pluginModule.default?.__selfTest
  if (!hooks || typeof hooks !== "object") {
    throw new SelfCheckError("idle_continue_hooks", "Plugin does not expose SmellPlugin.__selfTest.", {})
  }
  for (const key of [
    "idleContinueMode",
    "isOpendcodeRunMode",
    "isBatchEnvironment",
    "classifyFailureForContinue",
    "makeTaskKey",
    "buildContinuationMessage",
    "createIdleContinueRuntime",
    "SMELL_IDLE_CONTINUE_PREFIX",
    "MAX_IDLE_CONTINUE_ATTEMPTS",
    "REPAIRABLE_CATEGORIES",
  ]) {
    if (!(key in hooks)) {
      throw new SelfCheckError("idle_continue_hooks", `Plugin __selfTest is missing ${key}.`, {
        keys: Object.keys(hooks).sort(),
      })
    }
  }
  if (hooks.MAX_IDLE_CONTINUE_ATTEMPTS !== 2) {
    throw new SelfCheckError("idle_continue_hooks", "MAX_IDLE_CONTINUE_ATTEMPTS is not 2.", {
      value: hooks.MAX_IDLE_CONTINUE_ATTEMPTS,
    })
  }

  const runModeUnit = await runIsOpendcodeRunModeSelfCheck(hooks)

  function freshRuntime({ mode = "interactive", client, argv = ["/usr/local/bin/opencode", "tui"], extraEnv = {} } = {}) {
    const env = { SMELL_IDLE_CONTINUE_MODE: mode, ...extraEnv }
    return hooks.createIdleContinueRuntime({ client, env, argv })
  }

  function record(rt, { status = "SMELL_GUARD_FAILED", category = "SMELL_GUARD_FAILED", autoContinue = true, output, sessionID = "s1", taskKey = IDLE_TASK, agent = IDLE_AGENT, directory = IDLE_DIR } = {}) {
    const out = output || makeFailureOutput(status, category)
    return rt.recordFromBridgeOutput({ sessionID, agent, directory, taskKey, output: out, autoContinue })
  }

  // 1. off mode does not dispatch
  {
    const { client, calls } = makeFakeClient()
    const rt = freshRuntime({ mode: "off", client })
    record(rt, { autoContinue: true })
    const dispatched = rt.handleIdle("s1")
    await flush()
    assertEqual("off_no_dispatch_dispatched", dispatched, false, "dispatched")
    assertEqual("off_no_dispatch_calls", calls.length, 0, "calls")
    assertCond("off_metadata_disabled", record(rt, { autoContinue: true }).mode === "off", "off metadata mode")
  }

  // 2. shadow mode records but does not promptAsync
  {
    const { client, calls } = makeFakeClient()
    const rt = freshRuntime({ mode: "shadow", client })
    record(rt, { autoContinue: true })
    const dispatched = rt.handleIdle("s1")
    await flush()
    assertEqual("shadow_no_dispatch_dispatched", dispatched, false, "dispatched")
    assertEqual("shadow_no_dispatch_calls", calls.length, 0, "calls")
    assertCond("shadow_peek_exists", Boolean(rt.peek("s1")), "shadow should retain state")
  }

  // 3. interactive + autoContinue=true dispatches
  {
    const { client, calls } = makeFakeClient()
    const rt = freshRuntime({ client })
    record(rt, { autoContinue: true })
    const dispatched = rt.handleIdle("s1")
    await flush()
    assertEqual("interactive_dispatch_dispatched", dispatched, true, "dispatched")
    assertEqual("interactive_dispatch_calls", calls.length, 1, "calls")
    const body = calls[0].body
    assertCond("interactive_dispatch_agent", body && body.agent === IDLE_AGENT, "agent in body")
    assertCond("interactive_dispatch_parts", Array.isArray(body.parts) && body.parts[0].type === "text", "text part")
    assertCond("interactive_dispatch_query", calls[0].query && calls[0].query.directory === IDLE_DIR, "directory query")
    assertCond("interactive_dispatch_path", calls[0].path && calls[0].path.id === "s1", "path id")
  }

  // 4. autoContinue=false does not dispatch
  {
    const { client, calls } = makeFakeClient()
    const rt = freshRuntime({ client })
    record(rt, { autoContinue: false })
    const dispatched = rt.handleIdle("s1")
    await flush()
    assertEqual("no_autocontinue_dispatched", dispatched, false, "dispatched")
    assertEqual("no_autocontinue_calls", calls.length, 0, "calls")
  }

  // 5 & 6. first failure idle dispatches once; repeated idle stays at 1
  {
    const { client, calls } = makeFakeClient()
    const rt = freshRuntime({ client })
    record(rt, { autoContinue: true })
    rt.handleIdle("s1")
    await flush()
    assertEqual("first_idle_calls", calls.length, 1, "calls")
    rt.handleIdle("s1")
    rt.handleIdle("s1")
    await flush()
    assertEqual("repeated_idle_calls", calls.length, 1, "calls")
  }

  // 7 & 8. new failing verify allows round 2; round 3 hits the cap and stops
  {
    const { client, calls } = makeFakeClient()
    const rt = freshRuntime({ client })
    record(rt, { autoContinue: true }) // generation 1
    rt.handleIdle("s1")
    await flush()
    assertEqual("round1_calls", calls.length, 1, "calls")
    // Agent retried but failed again -> generation 2
    record(rt, { autoContinue: true })
    rt.handleIdle("s1")
    await flush()
    assertEqual("round2_calls", calls.length, 2, "calls")
    // Third failing verify -> generation 3, but attempt cap reached
    record(rt, { autoContinue: true })
    rt.handleIdle("s1")
    await flush()
    assertEqual("round3_cap_calls", calls.length, 2, "calls")
    const peeked = rt.peek("s1")
    assertCond("round3_attempt_cap", peeked && peeked.attempt === 2, "attempt must be 2")
  }

  // 9. PASS clears state
  {
    const { client, calls } = makeFakeClient()
    const rt = freshRuntime({ client })
    record(rt, { autoContinue: true })
    rt.handleIdle("s1")
    await flush()
    assertEqual("pass_before_calls", calls.length, 1, "calls")
    record(rt, { autoContinue: true, output: makePassOutput() })
    rt.handleIdle("s1")
    await flush()
    assertEqual("pass_after_calls", calls.length, 1, "calls")
    assertCond("pass_state_cleared", !rt.peek("s1"), "state should be cleared on PASS")
  }

  // 10. non-repairable failures do not dispatch
  for (const category of [
    "BUILD_TEST_REQUIRED",
    "SAMPLE_TEST_SPEC_MISSING",
    "RUNNER_VERIFY_FAILED",
    "MODEL_NO_VERIFY",
    "MODEL_NO_CONTEXT",
    "CONTEXT_FAILED",
    "OPENCODE_FAILED",
    "BUILD_DEPENDENCY_RESOLUTION",
    "TIMEOUT_OR_MODAL_SUSPECTED",
    "BUILD_TEST_FAILED",
    "UNKNOWN_VERIFY_FAILURE",
  ]) {
    const { client, calls } = makeFakeClient()
    const rt = freshRuntime({ client })
    record(rt, { status: category, category, autoContinue: true })
    rt.handleIdle("s1")
    await flush()
    assertEqual(`non_repairable_${category}_calls`, calls.length, 0, "calls")
  }
  // BUILD_FAILED is NOT repairable by itself
  {
    const { client, calls } = makeFakeClient()
    const rt = freshRuntime({ client })
    record(rt, { status: "BUILD_FAILED", category: "BUILD_FAILED", autoContinue: true })
    rt.handleIdle("s1")
    await flush()
    assertEqual("build_failed_raw_not_repairable", calls.length, 0, "calls")
  }
  // BUILD_FAILED is repairable only when classified as BUILD_COMPILE_ERROR
  {
    const { client, calls } = makeFakeClient()
    const rt = freshRuntime({ client })
    record(rt, { status: "BUILD_FAILED", category: "BUILD_COMPILE_ERROR", autoContinue: true })
    rt.handleIdle("s1")
    await flush()
    assertEqual("build_compile_error_repairable", calls.length, 1, "calls")
  }

  // 11. after a continuation without a new verify, no further dispatch
  {
    const { client, calls } = makeFakeClient()
    const rt = freshRuntime({ client })
    record(rt, { autoContinue: true })
    rt.handleIdle("s1")
    await flush()
    assertEqual("cont_noreverify_before", calls.length, 1, "calls")
    // No new smell_verify call -> pending is false after dispatch
    rt.handleIdle("s1")
    rt.handleIdle("s1")
    await flush()
    assertEqual("cont_noreverify_after", calls.length, 1, "calls")
  }

  // 12. new real user message resets state
  {
    const { client, calls } = makeFakeClient()
    const rt = freshRuntime({ client })
    record(rt, { autoContinue: true })
    const cleared = rt.handleChatMessage("s1", [{ type: "text", text: "please stop and try X instead" }])
    assertEqual("real_user_resets_cleared", cleared, true, "cleared")
    rt.handleIdle("s1")
    await flush()
    assertEqual("real_user_resets_calls", calls.length, 0, "calls")
  }

  // 13. plugin-injected [smell-auto-continue ...] message does NOT reset
  {
    const { client, calls } = makeFakeClient()
    const rt = freshRuntime({ client })
    record(rt, { autoContinue: true })
    const cleared = rt.handleChatMessage("s1", [{ type: "text", text: `${hooks.SMELL_IDLE_CONTINUE_PREFIX} 1/2] keep going` }])
    assertEqual("injected_msg_no_reset_cleared", cleared, false, "cleared")
    rt.handleIdle("s1")
    await flush()
    assertEqual("injected_msg_no_reset_calls", calls.length, 1, "calls")
  }

  // 14. batch environment does not dispatch
  for (const extra of [{ SMELL_BATCH_RUN: "1" }, { SMELL_PROJECT_ROOT: "/data/proj" }]) {
    const { client, calls } = makeFakeClient()
    const rt = freshRuntime({ client, extraEnv: extra })
    record(rt, { autoContinue: true })
    rt.handleIdle("s1")
    await flush()
    assertEqual(`batch_${Object.keys(extra)[0]}_calls`, calls.length, 0, "calls")
  }

  // 15. opencode run mode does not dispatch
  for (const argv of [["node", "/x/opencode", "run", "task"], ["/usr/local/bin/opencode", "run"]]) {
    const { client, calls } = makeFakeClient()
    const rt = hooks.createIdleContinueRuntime({ client, env: { SMELL_IDLE_CONTINUE_MODE: "interactive" }, argv })
    record(rt, { autoContinue: true })
    rt.handleIdle("s1")
    await flush()
    assertEqual(`runmode_${argv.join("_").slice(0, 20)}_calls`, calls.length, 0, "calls")
  }

  // 16. promptAsync failure -> safe stop, no retry
  {
    const { client, calls } = makeFailingFakeClient("boom")
    const rt = freshRuntime({ client })
    record(rt, { autoContinue: true })
    rt.handleIdle("s1")
    await flush()
    assertEqual("dispatch_fail_calls", calls.length, 1, "calls")
    rt.handleIdle("s1")
    await flush()
    assertEqual("dispatch_fail_no_retry", calls.length, 1, "calls")
    assertCond("dispatch_fail_error_recorded", Boolean(rt.getLastDispatchError()), "lastDispatchError should be set")
  }

  // 17. session.deleted and dispose clear state
  {
    const { client, calls } = makeFakeClient()
    const rt = freshRuntime({ client })
    record(rt, { autoContinue: true })
    rt.handleSessionDeleted("s1")
    assertCond("deleted_clears_state", !rt.peek("s1"), "state should be cleared on session.deleted")
    rt.handleIdle("s1")
    await flush()
    assertEqual("deleted_no_dispatch", calls.length, 0, "calls")
  }
  {
    const { client } = makeFakeClient()
    const rt = freshRuntime({ client })
    record(rt, { autoContinue: true })
    rt.dispose()
    assertCond("dispose_sets_flag", rt.isDisposed(), "disposed flag")
    assertCond("dispose_clears_state", !rt.peek("s1"), "state should be cleared on dispose")
    assertEqual("dispose_no_dispatch", rt.handleIdle("s1"), false, "dispatched after dispose")
  }

  // 18. message length bounded, secrets redacted, and artifact_paths (object)
  // are surfaced. Mirrors the real Python bridge contract.
  {
    const { client, calls } = makeFakeClient()
    const rt = freshRuntime({ client })
    record(rt, {
      autoContinue: true,
      category: "BUILD_COMPILE_ERROR",
      status: "BUILD_FAILED",
      output: makeFailureOutput("BUILD_FAILED", "BUILD_COMPILE_ERROR", {
        highlights: [
          "cannot find symbol Foo at line 42",
          "Authorization: Bearer sk-secret-token-1234567890abcdef",
          "api_key=gl-abcdef0123456789 in config",
          'config.api_key="shortsecret123" loaded',
          "Authorization: Basic abcdefghijklmnop",
          'TOKEN="smallsecret123"',
          "OPENCODE_API_KEY=sk-live-0987654321 along the log",
          "x".repeat(5000),
        ],
        artifact_paths: {
          build_log: "/tmp/art/build.log",
          test_log: "/tmp/art/test.log",
          verify_full: "/tmp/art/verify.json",
        },
      }),
    })
    rt.handleIdle("s1")
    await flush()
    assertEqual("bounded_msg_calls", calls.length, 1, "calls")
    const msg = calls[0].body.parts[0].text
    assertCond("bounded_msg_prefix", msg.startsWith(hooks.SMELL_IDLE_CONTINUE_PREFIX), "prefix present")
    assertCond("bounded_msg_length", msg.length <= 2100, `message too long: ${msg.length}`)
    assertCond("bounded_msg_has_status", msg.includes("Status:"), "message must include status")
    assertCond("bounded_msg_has_category", msg.includes("Failure category:"), "message must include category")
    // Secrets must be redacted: no bearer token value, no raw key value, no
    // env-var assignment value.
    assertCond("redact_no_bearer_token", !/sk-secret-token-1234567890abcdef/.test(msg), "bearer token leaked")
    assertCond("redact_no_apikey_value", !/gl-abcdef0123456789/.test(msg), "api_key value leaked")
    assertCond("redact_no_env_value", !/sk-live-0987654321/.test(msg), "env value leaked")
    assertCond("redact_has_redacted_marker", /\[REDACTED\]/.test(msg), "should contain REDACTED marker")
    // The real-code error line must survive redaction.
    assertCond("redact_keeps_code_hint", /cannot find symbol Foo/.test(msg), "code hint should survive")
    // Object artifact_paths must surface as paths in the message.
    assertCond("artifact_object_surfaced", /\/tmp\/art\/build\.log/.test(msg), "object artifact path missing")
  }

  // Non-allowed agent never dispatches
  {
    const { client, calls } = makeFakeClient()
    const rt = freshRuntime({ client })
    rt.recordFromBridgeOutput({
      sessionID: "s1",
      agent: "some-other-agent",
      directory: IDLE_DIR,
      taskKey: IDLE_TASK,
      output: makeFailureOutput("SMELL_GUARD_FAILED", "SMELL_GUARD_FAILED"),
      autoContinue: true,
    })
    rt.handleIdle("s1")
    await flush()
    assertEqual("non_allowed_agent_calls", calls.length, 0, "calls")
  }

  // REGRESSION (P1): the 2-round attempt budget is per-session and must NOT be
  // reset by a taskKey/location change. Two failures + two idle already used
  // the budget; a third failure with a DIFFERENT location must not dispatch.
  {
    const { client, calls } = makeFakeClient()
    const rt = freshRuntime({ client })
    record(rt, { autoContinue: true }) // gen 1
    rt.handleIdle("s1")
    await flush()
    assertEqual("budget_round1", calls.length, 1, "calls")
    record(rt, { autoContinue: true }) // gen 2
    rt.handleIdle("s1")
    await flush()
    assertEqual("budget_round2", calls.length, 2, "calls")
    // Attempt to bypass by changing location -> taskKey changes.
    record(rt, { autoContinue: true, taskKey: "/tmp/proj|feature_envy|Other.java:99" })
    rt.handleIdle("s1")
    await flush()
    assertEqual("budget_no_reset_on_taskkey", calls.length, 2, "calls")
    assertCond("budget_peek_attempt", rt.peek("s1") && rt.peek("s1").attempt === 2, "attempt must remain 2")
  }

  // REGRESSION (P1): a non-repairable result after a repairable one must revoke
  // the stale pending, so a subsequent idle does NOT resume on the old pack.
  {
    const { client, calls } = makeFakeClient()
    const rt = freshRuntime({ client })
    record(rt, { autoContinue: true, category: "SMELL_GUARD_FAILED" }) // arms pending
    assertCond("revoke_armed", rt.peek("s1") && rt.peek("s1").pending === true, "should be armed")
    record(rt, { autoContinue: true, category: "BUILD_DEPENDENCY_RESOLUTION" }) // non-repairable
    const peeked = rt.peek("s1")
    assertCond("revoke_pending_false", peeked && peeked.pending === false, "pending must be revoked")
    rt.handleIdle("s1")
    await flush()
    assertEqual("revoke_no_dispatch", calls.length, 0, "calls")
  }

  // REGRESSION (P1): autoContinue=false after a repairable result revokes
  // pending too.
  {
    const { client, calls } = makeFakeClient()
    const rt = freshRuntime({ client })
    record(rt, { autoContinue: true })
    record(rt, { autoContinue: false })
    const peeked = rt.peek("s1")
    assertCond("autofalse_revoke", peeked && peeked.pending === false, "pending must be revoked on autoContinue=false")
    rt.handleIdle("s1")
    await flush()
    assertEqual("autofalse_no_dispatch", calls.length, 0, "calls")
  }

  // REGRESSION (P1): non-JSON bridge output after a repairable result revokes
  // pending.
  {
    const { client, calls } = makeFakeClient()
    const rt = freshRuntime({ client })
    record(rt, { autoContinue: true })
    record(rt, { autoContinue: true, output: "Traceback: not json at all" })
    const peeked = rt.peek("s1")
    assertCond("nonjson_revoke", peeked && peeked.pending === false, "pending must be revoked on non-JSON")
    rt.handleIdle("s1")
    await flush()
    assertEqual("nonjson_no_dispatch", calls.length, 0, "calls")
  }

  // REGRESSION (P2): the real test-regression categories the bridge emits are
  // TEST_BEHAVIOR_REGRESSION and TEST_REFLECTION_ENTRY_STALE, not a literal
  // "TEST_FAILED". Both must be repairable.
  for (const category of ["TEST_BEHAVIOR_REGRESSION", "TEST_REFLECTION_ENTRY_STALE"]) {
    const { client, calls } = makeFakeClient()
    const rt = freshRuntime({ client })
    record(rt, { status: "TEST_FAILED", category, autoContinue: true })
    rt.handleIdle("s1")
    await flush()
    assertEqual(`real_test_category_${category}_calls`, calls.length, 1, "calls")
  }

  // REGRESSION (P2): artifact_paths object contract is accepted at the pure
  // classifyFailureForContinue level too (not only through the message).
  {
    const cls = hooks.classifyFailureForContinue({
      failure_category: "BUILD_COMPILE_ERROR",
      verify_status: "BUILD_FAILED",
      highlights: ["h1"],
      artifact_paths: { a: "/x/a.log", b: "/x/b.log" },
    })
    assertEqual("classify_object_artifact_len", cls.artifactPaths.length, 2, "artifactPaths length")
    assertCond("classify_object_artifact_ok", cls.ok === true, "should be repairable")
    const clsArr = hooks.classifyFailureForContinue({
      failure_category: "BUILD_COMPILE_ERROR",
      artifact_paths: ["/x/a.log"],
    })
    assertEqual("classify_array_artifact_len", clsArr.artifactPaths.length, 1, "array fallback length")
  }

  // REGRESSION (P1): redaction pure function covers common secret patterns,
  // including quoted values and Authorization scheme credentials, without
  // relying on a minimum-length heuristic.
  {
    assertCond("redact_helper_exists", typeof hooks.redactSecrets === "function", "redactSecrets must be exported")
    const redact = (txt) => hooks.redactSecrets(txt)
    // Reviewer counterexample 1: quoted api_key value (short).
    assertCond("redact_quoted_apikey_short", !/shortsecret123/.test(redact('api_key="shortsecret123"')), "quoted api_key value leaked")
    // Reviewer counterexample 2: Authorization: Basic <short cred>.
    assertCond("redact_auth_basic_short", !/abcdefghijklmnop/.test(redact("Authorization: Basic abcdefghijklmnop")), "Authorization Basic cred leaked")
    // Reviewer counterexample 3: short quoted TOKEN.
    assertCond("redact_quoted_token_short", !/smallsecret123/.test(redact('TOKEN="smallsecret123"')), "quoted TOKEN value leaked")
    // Original coverage still holds.
    assertCond("redact_bearer", !/secret-blob-value-1234567890/.test(redact("Authorization: Bearer secret-blob-value-1234567890")), "bearer not redacted")
    assertCond("redact_apikey", !/gl-live-key-9988776655/.test(redact("api_key=gl-live-key-9988776655")), "api_key not redacted")
    assertCond("redact_env_assign", !/supersecretvalue/.test(redact("MY_TOKEN=supersecretvalue here")), "env assign not redacted")
    assertCond("redact_single_quoted", !/innersecret/.test(redact("secret='innersecret'")), "single-quoted secret leaked")
    assertCond("redact_scheme_token", !/abc123/.test(redact("Token abc123 here")), "scheme token leaked")
    assertCond("redact_marker_present", /\[REDACTED\]/.test(redact("api_key=v")), "should contain REDACTED marker")
    assertCond("redact_keeps_text", /keep this/.test(redact("keep this plain text")), "non-secret text should survive")
    assertCond("redact_keeps_code_hint", /cannot find symbol Foo/.test(redact("cannot find symbol Foo at line 42")), "code hint should survive")
  }

  return {
    runModeUnit,
    maxAttempts: hooks.MAX_IDLE_CONTINUE_ATTEMPTS,
    repairableCategories: Array.from(hooks.REPAIRABLE_CATEGORIES).sort(),
    allowedAgents: Array.from(hooks.ALLOWED_AGENTS || []).sort(),
    passed: true,
  }
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
    Object.assign(process.env, cleanSmellIdentityEnv(process.env), { SMELL_ARTIFACT_ROOT: artifactRoot })
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
      const result = await smellVerify.execute({
        projectRoot: fixtureRoot,
        language: "java",
        smell: "long_method",
        location: "src/main/java/SelfCheckSample.java:2",
        verificationMode: "local",
        noSnapshot: true,
      })
      const successPath = normalizeToolResult(result)
      const normalizeUnit = await runPluginNormalizeSelfCheck(pluginModule)
      const failureIntegration = await runPluginFailureIntegrationSelfCheck(smellVerify)
      const idleContinue = await runIdleContinueSelfCheck(pluginModule)
      return { successPath, normalizeUnit, failureIntegration, idleContinue }
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

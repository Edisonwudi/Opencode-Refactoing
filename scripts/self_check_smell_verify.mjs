#!/usr/bin/env node
import { spawn } from "node:child_process"
import { chmod, mkdtemp, mkdir, readFile, readdir, rm, stat, symlink, writeFile } from "node:fs/promises"
import { existsSync } from "node:fs"
import os from "node:os"
import path from "node:path"
import { fileURLToPath, pathToFileURL } from "node:url"

const scriptFile = fileURLToPath(import.meta.url)
const root = path.resolve(path.dirname(scriptFile), "..")
const pluginFile = path.join(root, ".opencode", "plugins", "smell.ts")
const bridgeFile = path.join(root, "runtime", "python", "bridge", "smell_bridge.py")
const datasetRunnerFile = path.join(root, "scripts", "run_smell_dataset.py")
const sampleTestCommand = "java -cp target/test-classes SelfCheckSampleBehaviorTest"

function parseArgs(argv) {
  const options = {
    requireDataset: false,
    ideaProtocolOnly: false,
    guardProgressOnly: false,
    manualStateOnly: false,
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
    } else if (arg === "--guard-progress-only") {
      options.guardProgressOnly = true
    } else if (arg === "--manual-state-only") {
      options.manualStateOnly = true
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

async function runIdeaSkillProtocolDocSelfCheck() {
  const skillRoot = path.join(root, ".opencode", "skills", "idea-refactor-cli")
  const pathRoot = path.join(skillRoot, "references", "refactor-paths")
  const routeFiles = (await readdir(pathRoot))
    .filter((name) => name.endsWith(".yaml") || name.endsWith(".yml") || name.endsWith(".md"))
    .map((name) => path.join(pathRoot, name))
  const files = [
    path.join(skillRoot, "SKILL.md"),
    path.join(skillRoot, "references", "target-admission.md"),
    ...routeFiles,
  ]
  const forbidden = [
    {
      id: "direct_underlying_cli",
      pattern: /\bidea-refactor\s+(?:locate|prepare|apply)\b/gi,
    },
    {
      id: "legacy_locate_prepare_narrative",
      pattern: /\b(?:use locate|second locate|before prepare|prepare (?:returned|reports|exposed)|returned by prepare)\b|\blocators?\b[^\n]*\bprepare\b/gi,
    },
    {
      id: "flat_decision_shape",
      pattern: /decisions=\{\s*"[^"\n]+"\s*:\s*"/g,
    },
  ]
  const violations = []
  for (const file of files) {
    const source = await readFile(file, "utf8")
    for (const rule of forbidden) {
      for (const match of source.matchAll(rule.pattern)) {
        const line = source.slice(0, match.index).split("\n").length
        violations.push({
          rule: rule.id,
          file: path.relative(root, file),
          line,
          text: match[0],
        })
      }
    }
  }
  if (violations.length > 0) {
    throw new SelfCheckError(
      "idea_skill_protocol_docs",
      "Model-visible IDEA skill references contain legacy locate/prepare/apply protocol examples.",
      { violations },
    )
  }
  return {
    filesChecked: files.length,
    forbiddenPatterns: forbidden.map((rule) => rule.id),
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
    "SMELL_BUILD_COMMAND",
    "SMELL_PROJECT_TEST_COMMAND",
    "SMELL_VERIFICATION_CWD",
    "SMELL_VERIFICATION_COMMAND_SOURCE",
    "SMELL_SAMPLE_TEST_SOURCE",
    "SMELL_COMMAND_LOOP_STATE_JSON",
    "SMELL_BASELINE_SEAL",
    "SMELL_SESSION_STATE_ROOT",
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

function nearestNodeModules(start) {
  let current = path.resolve(start)
  while (true) {
    const candidate = path.join(current, "node_modules")
    if (existsSync(candidate)) return candidate
    const parent = path.dirname(current)
    if (parent === current) return ""
    current = parent
  }
}

async function readPackageVersion(packageRoot, packageName) {
  const nodeModules = nearestNodeModules(packageRoot)
  if (!nodeModules) return ""
  const packageJson = path.join(nodeModules, packageName, "package.json")
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
  const testSourceDir = path.join(fixtureRoot, "src", "test", "java")
  await mkdir(sourceDir, { recursive: true })
  await mkdir(testSourceDir, { recursive: true })
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
    path.join(testSourceDir, "SelfCheckSampleBehaviorTest.java"),
    [
      "import java.lang.reflect.Modifier;",
      "import java.nio.file.Files;",
      "import java.nio.file.Path;",
      "",
      "public class SelfCheckSampleBehaviorTest {",
      "  public static void main(String[] args) throws Exception {",
      "    var method = SelfCheckSample.class.getDeclaredMethod(\"add\", int.class, int.class);",
      "    if (!Modifier.isPublic(method.getModifiers())) {",
      "      throw new AssertionError(\"add must remain public\");",
      "    }",
      "    var report = Path.of(\".smell-test-reports\", \"TEST-SelfCheckSampleBehaviorTest.xml\");",
      "    Files.createDirectories(report.getParent());",
      "    Files.writeString(report, \"<testsuite name='SelfCheckSampleBehaviorTest' tests='1' failures='0' errors='0' skipped='0'><testcase name='publicAdd'/></testsuite>\\n\");",
      "    var ignoredOutput = Path.of(\"ignored-build\", \"controller.tmp\");",
      "    Files.createDirectories(ignoredOutput.getParent());",
      "    Files.writeString(ignoredOutput, \"controller verification output\\n\");",
      "    System.out.println(\"OK (1 test)\");",
      "  }",
      "}",
      "",
    ].join("\n"),
    "utf8",
  )
  await writeFile(path.join(fixtureRoot, ".gitignore"), "ignored-build/\n", "utf8")
  await writeFile(
    path.join(fixtureRoot, "projects.yaml"),
    [
      "projects:",
      `- root: ${JSON.stringify(fixtureRoot)}`,
      "  language: java",
      "  build:",
      "    command: \"mkdir -p target/test-classes && javac -d target/test-classes src/main/java/SelfCheckSample.java src/test/java/SelfCheckSampleBehaviorTest.java\"",
      "  test:",
      `    command: ${JSON.stringify(sampleTestCommand)}`,
      "",
    ].join("\n"),
    "utf8",
  )
  for (const args of [
    ["init", "-q"],
    ["add", ".gitignore", "src/main/java/SelfCheckSample.java", "src/test/java/SelfCheckSampleBehaviorTest.java", "projects.yaml"],
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
  env.SMELL_CHECKPOINT_ROOT = path.join(artifactRoot, "checkpoints")
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
    "--sample-test-command",
    sampleTestCommand,
  ]
  const baseline = await run(
    "python3",
    [bridgeFile, "capture-baseline", ...identityArgs],
    { cwd: fixtureRoot, env },
  )
  if (baseline.exitCode !== 0) {
    throw new SelfCheckError("bridge_capture_baseline", "Unable to capture fixture baseline.", baseline)
  }
  const baselinePayload = parseJson("bridge_capture_baseline", baseline.stdout)
  const baselineSeal = String(baselinePayload.baseline_seal || "")
  if (!baselineSeal) {
    throw new SelfCheckError(
      "bridge_capture_baseline",
      "Fixture baseline did not return its controller seal.",
      baseline,
    )
  }
  const baselinePlan = baselinePayload.resolution_plan || {}
  for (const forbiddenKey of ["worklist", "worklist_count", "files", "callers", "next_action"]) {
    assertCond(
      `bridge_baseline_plan_omits_${forbiddenKey}`,
      !Object.prototype.hasOwnProperty.call(baselinePlan, forbiddenKey),
      `baseline resolution_plan leaked ${forbiddenKey}`,
    )
  }
  const budgetSurface = await run(
    "python3",
    [
      "-c",
      [
        "import importlib.util, json, sys",
        "from pathlib import Path",
        "bridge = Path(sys.argv[1]).resolve()",
        "sys.path.insert(0, str(bridge.parents[1]))",
        "spec = importlib.util.spec_from_file_location('smell_bridge_budget_check', bridge)",
        "module = importlib.util.module_from_spec(spec)",
        "spec.loader.exec_module(module)",
        "value = {'route_family':'bounded-route','next_action':'leak','worklist':[{'file':'Leak.java'}],'files':['Leak.java'],'callers':['leak()'],'metric_budget':[{'metric':'method_lines','current':91,'passing_max':80,'passing_exclusive_max':999,'required_reduction':11,'unit':'lines','file':'Leak.java','caller':'leak()'}]}",
        "print(json.dumps(module._compact_baseline_resolution_plan(value), sort_keys=True))",
      ].join("; "),
      bridgeFile,
    ],
    { cwd: fixtureRoot, env },
  )
  assertEqual("bridge_metric_budget_surface_rc", budgetSurface.exitCode, 0, "exitCode")
  const compactBudgetPlan = parseJson("bridge_metric_budget_surface", budgetSurface.stdout)
  assertEqual(
    "bridge_metric_budget_plan_keys",
    Object.keys(compactBudgetPlan).sort().join(","),
    "metric_budget,route_family",
    "keys",
  )
  assertEqual(
    "bridge_metric_budget_item_keys",
    Object.keys(compactBudgetPlan.metric_budget?.[0] || {}).sort().join(","),
    "current,metric,passing_max,required_reduction,unit",
    "keys",
  )
  assertEqual(
    "bridge_metric_budget_prefers_inclusive_boundary",
    compactBudgetPlan.metric_budget?.[0]?.passing_max,
    80,
    "passing_max",
  )
  await writeFile(
    path.join(fixtureRoot, "candidate-note.txt"),
    "authored before controller verification\n",
    "utf8",
  )
  await mkdir(path.join(fixtureRoot, "ignored-build"), { recursive: true })
  const authoredIgnoredPath = path.join(fixtureRoot, "ignored-build", "authored.seed")
  await writeFile(authoredIgnoredPath, "pre-existing candidate input\n", "utf8")
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
  let result
  let payload
  let repeatedResult
  let repeatedPayload
  try {
    result = await run(
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
    if (result.exitCode !== 0) {
      throw new SelfCheckError("bridge_verify", "smell_bridge.py verify exited non-zero.", result)
    }
    payload = parseJson("bridge_verify_json", result.stdout)
    if (payload.status !== "PASS" || payload.success !== true) {
      throw new SelfCheckError("bridge_verify_status", "Bridge verification did not return PASS.", {
        status: payload.status,
        success: payload.success,
        payload,
      })
    }

    const postVerifyStatus = await run(
      "git",
      ["status", "--porcelain=v1", "--untracked-files=all"],
      { cwd: fixtureRoot },
    )
    assertEqual("bridge_verify_preserves_candidate_tree_rc", postVerifyStatus.exitCode, 0, "exitCode")
    assertEqual(
      "bridge_verify_preserves_candidate_tree",
      postVerifyStatus.stdout,
      " M src/main/java/SelfCheckSample.java\n?? candidate-note.txt\n",
      "gitStatus",
    )
    assertEqual(
      "bridge_verify_preserves_preexisting_ignored_input",
      await readFile(authoredIgnoredPath, "utf8"),
      "pre-existing candidate input\n",
      "content",
    )
    assertCond(
      "bridge_verify_removes_new_ignored_output",
      !existsSync(path.join(fixtureRoot, "ignored-build", "controller.tmp")),
      "controller-created ignored output remained in the candidate tree",
    )

    repeatedResult = await run(
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
    if (repeatedResult.exitCode !== 0) {
      throw new SelfCheckError(
        "bridge_repeated_verify",
        "Repeated smell_bridge.py verify exited non-zero.",
        repeatedResult,
      )
    }
    repeatedPayload = parseJson("bridge_repeated_verify_json", repeatedResult.stdout)
    if (repeatedPayload.status !== "PASS" || repeatedPayload.success !== true) {
      throw new SelfCheckError(
        "bridge_repeated_verify_status",
        "Repeated bridge verification did not return PASS.",
        {
          status: repeatedPayload.status,
          success: repeatedPayload.success,
          payload: repeatedPayload,
        },
      )
    }
  } finally {
    await writeFile(sourceFile, originalSource, "utf8")
  }
  return {
    exitCode: result.exitCode,
    status: payload.status,
    success: payload.success,
    repeatedStatus: repeatedPayload.status,
    repeatedSuccess: repeatedPayload.success,
    artifactKeys: Object.keys(payload.artifacts || {}).sort(),
    baselineResolutionPlanKeys: Object.keys(baselinePlan).sort(),
    metricBudgetSurface: compactBudgetPlan.metric_budget,
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
  const rootNodeModules = nearestNodeModules(root)
  if (rootNodeModules) {
    await symlink(rootNodeModules, path.join(tempRoot, "node_modules"), "dir")
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
  if (
    typeof hooks.normalizeToolResult !== "function" ||
    typeof hooks.buildBridgeOutputPayload !== "function" ||
    typeof hooks.normalizeBridgeContractPayload !== "function"
  ) {
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
      name: "bridge_empty_object_exit_zero_fails_closed",
      bridgeResult: {
        exitCode: 0,
        stdout: "{}",
        stderr: "",
        json: {},
      },
      expectStatus: "BRIDGE_CONTRACT_INVALID",
      expectSuccess: false,
    },
    {
      name: "bridge_pass_false_exit_zero_fails_closed",
      bridgeResult: {
        exitCode: 0,
        stdout: '{"success":false,"status":"PASS"}',
        stderr: "",
        json: { success: false, status: "PASS" },
      },
      expectStatus: "BRIDGE_CONTRACT_INVALID",
      expectSuccess: false,
    },
    {
      name: "bridge_lowercase_pass_exit_zero_fails_closed",
      bridgeResult: {
        exitCode: 0,
        stdout: '{"success":true,"status":"pass"}',
        stderr: "",
        json: { success: true, status: "pass" },
      },
      expectStatus: "BRIDGE_CONTRACT_INVALID",
      expectSuccess: false,
    },
    {
      name: "bridge_failure_status_true_exit_zero_fails_closed",
      bridgeResult: {
        exitCode: 0,
        stdout: '{"success":true,"status":"SMELL_GUARD_FAILED"}',
        stderr: "",
        json: { success: true, status: "SMELL_GUARD_FAILED" },
      },
      expectStatus: "BRIDGE_CONTRACT_INVALID",
      expectSuccess: false,
    },
    {
      name: "bridge_nonpass_success_exit_zero_fails_closed",
      bridgeResult: {
        exitCode: 0,
        stdout: '{"success":true,"status":"BASELINE_CAPTURED"}',
        stderr: "",
        json: { success: true, status: "BASELINE_CAPTURED" },
      },
      expectStatus: "BRIDGE_CONTRACT_INVALID",
      expectSuccess: false,
    },
    {
      name: "bridge_pass_payload_nonzero_exit_fails_closed",
      bridgeResult: {
        exitCode: 7,
        stdout: '{"success":true,"status":"PASS"}',
        stderr: "bridge terminated after emitting output",
        json: { success: true, status: "PASS" },
      },
      expectStatus: "BRIDGE_FAILED",
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

async function runIdeaBackendSurfaceSelfCheck(pluginModule) {
  const envBefore = { ...process.env }
  const restoreEnv = () => {
    for (const key of Object.keys(process.env)) {
      if (!(key in envBefore)) delete process.env[key]
    }
    Object.assign(process.env, envBefore)
  }
  try {
    process.env.SMELL_REFACTORING_BACKEND = "idea"
    process.env.SMELL_ENABLE_IDEA_TOOLS = "1"
    const ideaPlugin = await pluginModule.SmellPlugin({ worktree: "/tmp/idea-backend-self-check" })
    const toolKeys = Object.keys(ideaPlugin?.tool || {}).sort()
    for (const required of ["smell_verify", "idea_refactor_preview", "idea_refactor_apply", "idea_edit"]) {
      if (!toolKeys.includes(required)) {
        throw new SelfCheckError("idea_backend_tool_surface", `IDEA backend did not expose ${required}.`, { toolKeys })
      }
    }
    const beforeHook = ideaPlugin?.["tool.execute.before"]
    if (typeof beforeHook !== "function") {
      throw new SelfCheckError("idea_backend_tool_hook", "IDEA backend did not register tool.execute.before.", {})
    }
    let unownedIdeaRejected = false
    try {
      await beforeHook(
        { tool: "idea_refactor_preview", sessionID: "idea-session" },
        { args: { operation: "rename:method", target: { fqcn: "example.Foo", memberName: "oldName" } } },
      )
    } catch (error) {
      unownedIdeaRejected = String(error).includes("IDEA_COMMAND_POLICY_REQUIRED")
    }
    if (!unownedIdeaRejected) {
      throw new SelfCheckError("idea_backend_policy_authority", "Process environment enabled an unowned IDEA tool call.", {
        unownedIdeaRejected,
      })
    }
    // Process-wide backend variables are not command authority. Ordinary tools
    // remain untouched until a command freezes refactoring_backend=idea.
    await beforeHook(
      { tool: "edit", sessionID: "idea-session" },
      { args: { filePath: "/tmp/idea-backend-self-check/src/Foo.java" } },
    )
    await beforeHook(
      { tool: "bash", sessionID: "idea-session" },
      { args: { command: "/usr/local/bin/idea-refactor locate --project-root /tmp/p" } },
    )
    await beforeHook(
      { tool: "read", sessionID: "idea-session" },
      { args: { filePath: "/tmp/idea-backend-self-check/src/Foo.java" } },
    )
    await ideaPlugin?.dispose?.()

    process.env.SMELL_REFACTORING_BACKEND = "direct"
    process.env.SMELL_ENABLE_IDEA_TOOLS = "1"
    const directPlugin = await pluginModule.SmellPlugin({ worktree: "/tmp/direct-backend-self-check" })
    const directToolKeys = Object.keys(directPlugin?.tool || {}).sort()
    if (!directToolKeys.some((name) => name === "idea_refactor_preview")) {
      throw new SelfCheckError("static_idea_tool_surface", "Static plugin surface omitted IDEA tools before command policy resolution.", {
        toolKeys: directToolKeys,
      })
    }
    await directPlugin?.dispose?.()
    return {
      ideaToolKeys: toolKeys.filter((name) => name.startsWith("idea_")),
      directBackendIdeaTools: directToolKeys.filter((name) => name.startsWith("idea_")),
      environmentIsNotAuthority: unownedIdeaRejected,
    }
  } finally {
    restoreEnv()
  }
}

async function runIdeaManualCommandProtocolSelfCheck() {
  const tempRoot = await mkdtemp(path.join(os.tmpdir(), "idea-command-protocol-self-check-"))
  const envBefore = { ...process.env }
  try {
    const projectRoot = path.join(tempRoot, "project")
    const stateRoot = path.join(tempRoot, "state")
    const fakeBridge = path.join(tempRoot, "bridge.py")
    const fakeCli = path.join(tempRoot, "idea-refactor")
    const cliLog = path.join(tempRoot, "idea-cli.jsonl")
    await mkdir(path.join(projectRoot, "src"), { recursive: true })
    await writeFile(path.join(projectRoot, "src", "Foo.java"), "class Foo {}\n", "utf8")
    await writeFile(cliLog, "", "utf8")
    await writeFile(fakeBridge, `
import json
import os
import sys

command = sys.argv[1]
project_root = os.environ["IDEA_SELF_CHECK_PROJECT_ROOT"]
identity = {
    "project_root": project_root,
    "project_override_root": "",
    "language": "java",
    "smell": "mysterious_name",
    "location": "src/Foo.java:1",
    "target_context_json": "",
    "verification_mode": "project_full",
    "sample_test_location": "",
    "sample_test_command": "",
    "build_command": "",
    "project_test_command": "",
    "verification_cwd": "",
    "verification_command_source": "",
    "sample_test_source": "",
}
if command == "resolve-command":
    payload = {
        "task": "Continue the current Java refactoring task.",
        "verification_mode": "project_full",
        "refactoring_backend": "idea",
        "allow_test_changes": False,
        "checkpoint_required": False,
        "identity": identity,
        "loop": {
            "mode": "verify-failure",
            "max_smell_verify_cycles": 2,
            "no_progress_limit": 1,
            "allowed_failure_groups": ["smell", "compile", "test"],
            "instruction": "repair narrowly",
            "sample_deadline_seconds": 60,
        },
    }
elif command == "verify":
    payload = {
        "success": True,
        "accepted": True,
        "progress": True,
        "project_full_executed": True,
        "status": "PASS",
        "resolution": "resolved",
        "formal_verification_receipt": {
            "schema_version": "smell.formal-verification-receipt/v1",
            "terminal_stage": "formal_verify",
            "status": "PASS",
            "success": True,
            "accepted": True,
            "resolution": "resolved",
            "candidate_identity": {
                "baseline_revision": "idea-baseline",
                "baseline_tree": "idea-tree",
                "production_diff": "idea-production-diff",
                "test_tree": "idea-test-tree",
                "verification_config_tree": "idea-config-tree",
            },
            "outcome": "pass",
            "diagnostic_signature": "PASS",
            "guard": {"success": True, "failure_count": 0},
            "build_test": {"success": True, "project_full_executed": True, "test_status": "passed"},
            "fresh_isolation": None,
            "artifact_refs": {},
        },
    }
else:
    payload = {"success": False, "status": "UNEXPECTED_COMMAND"}
print(json.dumps(payload))
`, "utf8")
    await writeFile(fakeCli, `#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
with Path(os.environ["IDEA_SELF_CHECK_CLI_LOG"]).open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(args) + "\\n")
action = args[0] if args else ""
if action == "ensure-service":
    if os.environ.get("IDEA_SELF_CHECK_PRECHECK_FAIL") == "1":
        print(json.dumps({"status": "failed", "diagnostics": [{"code": "SERVICE_NOT_READY"}]}))
        raise SystemExit(1)
    payload = {"status": "ok"}
elif action == "locate":
    payload = {
        "status": "ok",
        "draftId": "draft-1",
        "availableOperations": [{"operation": "rename:method"}],
    }
elif action == "prepare":
    payload = {"status": "ok", "operation": "rename:method"}
elif action == "apply":
    payload = {"status": "ok", "applied": True, "operation": "rename:method", "result": {"changedFilePaths": ["src/Foo.java"]}}
elif action == "edit":
    payload = {"status": "ok"}
elif action == "rollback":
    payload = {"status": "ok"}
else:
    payload = {"status": "failed", "diagnostics": [{"code": "UNEXPECTED_ACTION"}]}
print(json.dumps(payload))
`, "utf8")
    await chmod(fakeCli, 0o755)
    Object.assign(process.env, cleanSmellIdentityEnv(process.env), {
      SMELL_BRIDGE_FILE: fakeBridge,
      SMELL_IDEA_REFACTOR_CLI: fakeCli,
      SMELL_SESSION_STATE_ROOT: stateRoot,
      IDEA_SELF_CHECK_PROJECT_ROOT: projectRoot,
      IDEA_SELF_CHECK_CLI_LOG: cliLog,
    })
    delete process.env.SMELL_BATCH_RUN
    const compiledFile = await compilePluginForSelfCheck(tempRoot)
    const pluginModule = await import(`${pathToFileURL(compiledFile).href}?idea_command=${Date.now()}`)
    const hooks = pluginModule.SmellPlugin.__selfTest
    const plugin = await pluginModule.SmellPlugin({ worktree: projectRoot })
    const sessionID = "idea-manual-command"
    await plugin["command.execute.before"](
      {
        command: "java-refactor-run",
        sessionID,
        arguments: `--refactoring-backend=idea -- Project root: ${projectRoot}; Language: java; Smell type: mysterious_name; Target location: src/Foo.java:1`,
      },
      { parts: [] },
    )
    const cliCallsAfterPrecheck = (await readFile(cliLog, "utf8")).trim().split("\n").filter(Boolean).map(JSON.parse)
    assertEqual("idea_manual_precheck_action", cliCallsAfterPrecheck[0]?.[0], "ensure-service", "action")
    assertCond("idea_manual_precheck_opens_project", cliCallsAfterPrecheck[0]?.includes("--open"), JSON.stringify(cliCallsAfterPrecheck[0]))
    assertCond("idea_manual_precheck_has_shared_timeout", Number(cliCallsAfterPrecheck[0]?.[cliCallsAfterPrecheck[0].indexOf("--timeout") + 1]) > 0, JSON.stringify(cliCallsAfterPrecheck[0]))

    const beforeHook = plugin["tool.execute.before"]
    for (const [name, toolName, args, code] of [
      ["direct_edit", "edit", { filePath: path.join(projectRoot, "src", "Foo.java") }, "IDEA_BACKEND_DIRECT_EDIT_FORBIDDEN"],
      ["shell", "bash", { command: "pwd" }, "IDEA_BACKEND_SHELL_FORBIDDEN"],
      ["root_retarget", "idea_refactor_preview", { projectRoot: path.join(tempRoot, "other"), operation: "rename:method", target: { fqcn: "Foo" } }, "IDEA_PROJECT_ROOT_MISMATCH"],
      ["apply_before_preview", "idea_refactor_apply", { proposalId: "draft-1" }, "IDEA_APPLY_REQUIRES_READY_PROPOSAL"],
    ]) {
      let message = ""
      try {
        await beforeHook({ tool: toolName, sessionID }, { args })
      } catch (error) {
        message = String(error?.message || error)
      }
      assertCond(`idea_manual_${name}_blocked`, message.includes(code), message)
    }

    const preview = await plugin.tool.idea_refactor_preview.execute(
      { operation: "rename:method", target: { fqcn: "Foo", memberName: "oldName", parameterTypes: [] } },
      { sessionID, agent: "java-refactor-agent", directory: projectRoot },
    )
    assertEqual("idea_manual_preview_ready", JSON.parse(preview.output).status, "ready", "status")
    let wrongProposal = ""
    try {
      await plugin.tool.idea_refactor_apply.execute(
        { proposalId: "draft-2" },
        { sessionID, agent: "java-refactor-agent", directory: projectRoot },
      )
    } catch (error) {
      wrongProposal = String(error?.message || error)
    }
    assertCond("idea_manual_proposal_id_frozen", wrongProposal.includes("IDEA_PROPOSAL_ID_MISMATCH"), wrongProposal)
    const apply = await plugin.tool.idea_refactor_apply.execute(
      { proposalId: "draft-1" },
      { sessionID, agent: "java-refactor-agent", directory: projectRoot },
    )
    assertEqual("idea_manual_apply_succeeds", JSON.parse(apply.output).status, "applied", "status")
    const verified = await plugin.tool.smell_verify.execute(
      { projectRoot, language: "java", smell: "mysterious_name", location: "src/Foo.java:1", verificationMode: "project_full" },
      { sessionID, agent: "java-refactor-agent", directory: projectRoot },
    )
    const verifiedPayload = JSON.parse(verified.output)
    assertEqual("idea_manual_verify_pass", verifiedPayload.status, "PASS", "status")
    assertEqual("idea_manual_receipt_complete", verifiedPayload.idea_protocol_receipt?.complete, true, "complete")
    assertEqual("idea_manual_receipt_route", verifiedPayload.idea_protocol_receipt?.mutation_route, "native_apply", "route")
    const envelope = JSON.parse(await readFile(hooks.commandSessionStateFile(sessionID), "utf8"))
    assertEqual("idea_manual_backend_persisted", envelope.command_loop_state.policy.refactoring_backend, "idea", "backend")
    assertEqual("idea_manual_state_verified", envelope.command_loop_state.idea_protocol_state.verified_generation, 1, "verified_generation")
    assertEqual("idea_manual_terminal_receipt_complete", envelope.command_loop_state.terminal_receipt.ideaProtocolReceipt.complete, true, "complete")
    const mismatchedReceiptState = JSON.parse(JSON.stringify(envelope.command_loop_state))
    mismatchedReceiptState.terminal_receipt.ideaProtocolReceipt.verified_generation = 0
    assertEqual(
      "idea_manual_terminal_receipt_must_match_state",
      hooks.restoreCommandLoopState(JSON.stringify(mismatchedReceiptState)),
      undefined,
      "restored state",
    )

    const editSessionID = "idea-authorized-edit-command"
    await plugin["command.execute.before"](
      {
        command: "java-refactor-run",
        sessionID: editSessionID,
        arguments: `--refactoring-backend=idea -- Project root: ${projectRoot}; Language: java; Smell type: mysterious_name; Target location: src/Foo.java:1`,
      },
      { parts: [] },
    )
    const unsupported = await plugin.tool.idea_refactor_preview.execute(
      { operation: "move:method", target: { fqcn: "Foo", memberName: "oldName", parameterTypes: [] } },
      { sessionID: editSessionID, agent: "java-refactor-agent", directory: projectRoot },
    )
    assertEqual("idea_manual_edit_requires_real_blocker", JSON.parse(unsupported.output).status, "unsupported_target", "status")
    const edited = await plugin.tool.idea_edit.execute(
      { file: "src/Foo.java", oldString: "class Foo {}", newString: "class Foo { }" },
      { sessionID: editSessionID, agent: "java-refactor-agent", directory: projectRoot },
    )
    assertEqual("idea_manual_authorized_edit_succeeds", JSON.parse(edited.output).success, true, "success")
    const editVerified = await plugin.tool.smell_verify.execute(
      { projectRoot, language: "java", smell: "mysterious_name", location: "src/Foo.java:1", verificationMode: "project_full" },
      { sessionID: editSessionID, agent: "java-refactor-agent", directory: projectRoot },
    )
    const editVerifiedPayload = JSON.parse(editVerified.output)
    assertEqual("idea_manual_authorized_edit_receipt", editVerifiedPayload.idea_protocol_receipt?.mutation_route, "authorized_edit", "route")
    assertEqual("idea_manual_authorized_edit_blocker_receipt", editVerifiedPayload.idea_protocol_receipt?.blocker_status, "unsupported_target", "blocker_status")

    process.env.IDEA_SELF_CHECK_PRECHECK_FAIL = "1"
    let precheckFailure = ""
    try {
      await plugin["command.execute.before"](
        {
          command: "java-refactor-run",
          sessionID: "idea-precheck-failure",
          arguments: `--refactoring-backend=idea -- Project root: ${projectRoot}; Language: java; Smell type: mysterious_name; Target location: src/Foo.java:1`,
        },
        { parts: [] },
      )
    } catch (error) {
      precheckFailure = String(error?.message || error)
    }
    assertCond("idea_manual_precheck_failure_terminal", precheckFailure.includes("IDEA_PRECHECK_FAILED"), precheckFailure)
    const failedEnvelope = JSON.parse(await readFile(hooks.commandSessionStateFile("idea-precheck-failure"), "utf8"))
    assertEqual("idea_manual_precheck_failure_stage", failedEnvelope.command_loop_state.terminal_receipt.stage, "protocol", "stage")
    assertEqual("idea_manual_precheck_failure_status", failedEnvelope.command_loop_state.terminal_receipt.status, "IDEA_PRECHECK_FAILED", "status")
    await plugin.dispose?.()
    return {
      precheckBeforeModel: true,
      frozenBackendAndRoot: true,
      proposalIdBound: true,
      unsupportedTargetAuthorizesEdit: true,
      verifiedReceipt: true,
      precheckFailureTerminal: true,
    }
  } finally {
    for (const key of Object.keys(process.env)) {
      if (!(key in envBefore)) delete process.env[key]
    }
    Object.assign(process.env, envBefore)
    await rm(tempRoot, { recursive: true, force: true })
  }
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
  for (const name of [
    "runIdeaPreviewProtocol",
    "renderIdeaApplyProtocolResult",
    "ideaDecisionsShape",
    "newIdeaProtocolState",
    "recordIdeaPreviewOutcome",
    "assertIdeaApplyAllowed",
    "recordIdeaApplyOutcome",
    "assertIdeaEditAllowed",
    "recordIdeaEditOutcome",
    "assertIdeaVerifyAllowed",
    "recordIdeaVerifyOutcome",
    "assertIdeaRevertAllowed",
    "recordIdeaRevertOutcome",
    "ideaProtocolReceipt",
  ]) {
    if (typeof hooks[name] !== "function") {
      throw new SelfCheckError("plugin_self_test_hooks", `Plugin __selfTest is missing ${name}.`, {
        keys: Object.keys(hooks).sort(),
      })
    }
  }
  const decisionSchema = hooks.ideaDecisionsShape("self-check decisions")
  const validDecisionShape = decisionSchema.safeParse({
    "selection.extract-method.scope": { choice: "selection_0", arguments: {} },
  })
  const invalidDecisionShape = decisionSchema.safeParse({
    "selection.extract-method.scope": "selection_0",
  })
  if (!validDecisionShape.success || invalidDecisionShape.success) {
    throw new SelfCheckError("plugin_idea_decision_schema", "IDEA decision schema accepted the wrong value shape.", {
      validAccepted: validDecisionShape.success,
      invalidAccepted: invalidDecisionShape.success,
    })
  }
  const expectProtocolError = (name, expectedCode, action) => {
    let message = ""
    try {
      action()
    } catch (error) {
      message = String(error?.message || error)
    }
    if (!message.includes(expectedCode)) {
      throw new SelfCheckError("plugin_idea_command_protocol", `${name} did not fail with ${expectedCode}.`, {
        message,
      })
    }
  }

  const applyBeforePreview = hooks.newIdeaProtocolState()
  expectProtocolError(
    "apply_before_preview",
    "IDEA_APPLY_REQUIRES_READY_PROPOSAL",
    () => hooks.assertIdeaApplyAllowed(applyBeforePreview, "draft-1"),
  )

  const proposalState = hooks.newIdeaProtocolState()
  hooks.recordIdeaPreviewOutcome(
    proposalState,
    { operation: "rename:method", proposalId: "" },
    {
      protocol: "idea-proposal-v1",
      status: "ready",
      proposalId: "draft-1",
      operation: "rename:method",
      diagnostics: [],
    },
  )
  expectProtocolError(
    "apply_proposal_mismatch",
    "IDEA_PROPOSAL_ID_MISMATCH",
    () => hooks.assertIdeaApplyAllowed(proposalState, "draft-2"),
  )
  hooks.assertIdeaApplyAllowed(proposalState, "draft-1")
  hooks.recordIdeaApplyOutcome(proposalState, "draft-1", {
    protocol: "idea-proposal-v1",
    status: "applied",
    proposalId: "draft-1",
    operation: "rename:method",
    diagnostics: [],
  })
  hooks.assertIdeaVerifyAllowed(proposalState, { controlGeneration: 1, confirmationRequired: false })
  hooks.recordIdeaVerifyOutcome(proposalState)
  expectProtocolError(
    "repeat_verify_without_mutation",
    "IDEA_VERIFY_REQUIRES_NEW_MUTATION",
    () => hooks.assertIdeaVerifyAllowed(proposalState, { controlGeneration: 1, confirmationRequired: false }),
  )
  hooks.assertIdeaVerifyAllowed(proposalState, { controlGeneration: 1, confirmationRequired: true })
  const nativeReceipt = hooks.ideaProtocolReceipt(proposalState)
  if (
    nativeReceipt?.schema_version !== "smell.idea-protocol-receipt/v1"
    || nativeReceipt.mutation_route !== "native_apply"
    || nativeReceipt.mutation_generation !== 1
    || nativeReceipt.verified_generation !== 1
    || nativeReceipt.proposal_id !== "draft-1"
  ) {
    throw new SelfCheckError("plugin_idea_command_protocol", "Native apply receipt was not bound to its verification.", {
      nativeReceipt,
    })
  }

  const failedApplyState = hooks.newIdeaProtocolState()
  hooks.recordIdeaPreviewOutcome(
    failedApplyState,
    { operation: "rename:method", proposalId: "" },
    { protocol: "idea-proposal-v1", status: "ready", proposalId: "draft-failed", operation: "rename:method", diagnostics: [] },
  )
  hooks.recordIdeaApplyOutcome(failedApplyState, "draft-failed", {
    protocol: "idea-proposal-v1",
    status: "failed",
    proposalId: "draft-failed",
    operation: "rename:method",
    diagnostics: [{ code: "REFACTORING_FAILED" }],
  })
  expectProtocolError(
    "failed_apply_does_not_authorize_verify",
    "IDEA_VERIFY_REQUIRES_MUTATION",
    () => hooks.assertIdeaVerifyAllowed(failedApplyState, { controlGeneration: 1, confirmationRequired: false }),
  )
  expectProtocolError(
    "failed_apply_does_not_authorize_edit",
    "IDEA_EDIT_REQUIRES_UNSUPPORTED_TARGET",
    () => hooks.assertIdeaEditAllowed(failedApplyState),
  )

  const blockerState = hooks.newIdeaProtocolState()
  expectProtocolError(
    "edit_without_blocker",
    "IDEA_EDIT_REQUIRES_UNSUPPORTED_TARGET",
    () => hooks.assertIdeaEditAllowed(blockerState),
  )
  hooks.recordIdeaPreviewOutcome(
    blockerState,
    { operation: "move:method", proposalId: "" },
    {
      protocol: "idea-proposal-v1",
      status: "unsupported_target",
      proposalId: "draft-blocked",
      operation: "move:method",
      diagnostics: [{ code: "OPERATION_UNAVAILABLE" }],
    },
  )
  hooks.assertIdeaEditAllowed(blockerState)
  hooks.recordIdeaEditOutcome(blockerState, { success: true, status: "ok" })
  hooks.assertIdeaVerifyAllowed(blockerState, { controlGeneration: 1, confirmationRequired: false })
  hooks.recordIdeaVerifyOutcome(blockerState)
  const fallbackReceipt = hooks.ideaProtocolReceipt(blockerState)
  if (
    fallbackReceipt?.mutation_route !== "authorized_edit"
    || fallbackReceipt.blocker_status !== "unsupported_target"
    || fallbackReceipt.mutation_generation !== 1
  ) {
    throw new SelfCheckError("plugin_idea_command_protocol", "Authorized edit receipt lost its proposal blocker.", {
      fallbackReceipt,
    })
  }

  const infraFailureState = hooks.newIdeaProtocolState()
  hooks.recordIdeaPreviewOutcome(
    infraFailureState,
    { operation: "rename:method", proposalId: "" },
    {
      protocol: "idea-proposal-v1",
      status: "failed",
      proposalId: "",
      operation: "rename:method",
      diagnostics: [{ code: "IDEA_CLI_OUTPUT_PARSE_FAILED" }],
    },
  )
  expectProtocolError(
    "infrastructure_failure_is_not_edit_authority",
    "IDEA_EDIT_REQUIRES_UNSUPPORTED_TARGET",
    () => hooks.assertIdeaEditAllowed(infraFailureState),
  )

  const uncertainState = hooks.newIdeaProtocolState()
  hooks.recordIdeaPreviewOutcome(
    uncertainState,
    { operation: "rename:method", proposalId: "" },
    { protocol: "idea-proposal-v1", status: "ready", proposalId: "draft-unknown", operation: "rename:method", diagnostics: [] },
  )
  hooks.recordIdeaApplyOutcome(uncertainState, "draft-unknown", {
    protocol: "idea-proposal-v1",
    status: "outcome_unknown",
    proposalId: "draft-unknown",
    operation: "rename:method",
    diagnostics: [{ code: "SERVICE_REQUEST_TIMEOUT" }],
  })
  expectProtocolError(
    "unknown_apply_must_not_repeat",
    "IDEA_APPLY_REQUIRES_READY_PROPOSAL",
    () => hooks.assertIdeaApplyAllowed(uncertainState, "draft-unknown"),
  )
  hooks.assertIdeaVerifyAllowed(uncertainState, { controlGeneration: 1, confirmationRequired: false })

  const revertState = hooks.newIdeaProtocolState()
  expectProtocolError(
    "revert_without_apply",
    "IDEA_REVERT_REQUIRES_COMMAND_APPLY",
    () => hooks.assertIdeaRevertAllowed(revertState),
  )
  hooks.recordIdeaPreviewOutcome(
    revertState,
    { operation: "rename:method", proposalId: "" },
    { protocol: "idea-proposal-v1", status: "ready", proposalId: "draft-revert", operation: "rename:method", diagnostics: [] },
  )
  hooks.recordIdeaApplyOutcome(revertState, "draft-revert", {
    protocol: "idea-proposal-v1",
    status: "applied",
    proposalId: "draft-revert",
    operation: "rename:method",
    diagnostics: [],
  })
  hooks.assertIdeaRevertAllowed(revertState)
  hooks.recordIdeaRevertOutcome(revertState, { success: true, status: "ok" })
  expectProtocolError(
    "revert_only_once",
    "IDEA_REVERT_REQUIRES_COMMAND_APPLY",
    () => hooks.assertIdeaRevertAllowed(revertState),
  )
  expectProtocolError(
    "reverted_candidate_cannot_verify",
    "IDEA_VERIFY_REQUIRES_MUTATION",
    () => hooks.assertIdeaVerifyAllowed(revertState, { controlGeneration: 1, confirmationRequired: false }),
  )
  let invalidTargetRunnerCalls = 0
  const invalidContinuationPayload = JSON.parse((await hooks.runIdeaPreviewProtocol({
    worktree: "/p",
    cli: "idea-refactor",
    request: {
      projectRoot: "/p",
      operation: "rename:method",
      proposalId: "draft-conflict",
      target: { fqcn: "demo.Foo", memberName: "run", parameterTypes: [] },
      arguments: { newName: "execute" },
    },
    runner: async () => {
      invalidTargetRunnerCalls += 1
      throw new Error("invalid preview must not invoke the CLI")
    },
  })).output)
  if (
    invalidContinuationPayload.status !== "needs_input"
    || invalidContinuationPayload.nextAction !== "preview"
    || invalidContinuationPayload.nextRequest?.tool !== "idea_refactor_preview"
    || invalidContinuationPayload.nextRequest?.args?.proposalId !== "draft-conflict"
    || invalidContinuationPayload.nextRequest?.args?.target !== undefined
    || invalidContinuationPayload.nextRequest?.args?.file !== undefined
  ) {
    throw new SelfCheckError("plugin_idea_invalid_target_recovery", "Continuation conflict did not return a legal retry.", {
      payload: invalidContinuationPayload,
    })
  }
  const invalidInitialPayload = JSON.parse((await hooks.runIdeaPreviewProtocol({
    worktree: "/p",
    cli: "idea-refactor",
    request: {
      projectRoot: "/p",
      operation: "rename:method",
      target: { fqcn: "demo.Foo", memberName: "run", parameterTypes: [] },
      file: "/p/src/Foo.java",
      line: 4,
      column: 5,
    },
    runner: async () => {
      invalidTargetRunnerCalls += 1
      throw new Error("invalid preview must not invoke the CLI")
    },
  })).output)
  if (
    invalidInitialPayload.status !== "needs_input"
    || invalidInitialPayload.nextAction !== "preview"
    || invalidInitialPayload.nextRequest?.args?.target?.fqcn !== "demo.Foo"
    || invalidInitialPayload.nextRequest?.args?.file !== undefined
    || invalidTargetRunnerCalls !== 0
  ) {
    throw new SelfCheckError("plugin_idea_invalid_target_recovery", "Initial target conflict did not return a legal retry.", {
      payload: invalidInitialPayload,
      runnerCalls: invalidTargetRunnerCalls,
    })
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
        nextCliCommandExample: {
          action: "apply",
          argumentsJson: { newName: "execute" },
          decisionsJson: {},
        },
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
  if (JSON.stringify(previewPayload.nextRequest) !== JSON.stringify({
    tool: "idea_refactor_apply",
    args: {
      ideaProjectRoot: "/p",
      proposalId: "draft-1",
      arguments: { newName: "execute" },
      decisions: {},
    },
  })) {
    previewErrors.push(`ready nextRequest=${JSON.stringify(previewPayload.nextRequest)}`)
  }
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

  const locateRetryPayload = JSON.parse((await hooks.runIdeaPreviewProtocol({
    worktree: "/p",
    cli: "idea-refactor",
    request: {
      projectRoot: "/p",
      operation: "rename:method",
      target: { fqcn: "demo.Foo", memberName: "run", parameterTypes: [] },
      arguments: { newName: "execute" },
    },
    runner: async (_worktree, _cli, argv) => ({
      exitCode: 3,
      stdout: "",
      stderr: "",
      argv,
      json: {
        status: "retryable_failed",
        diagnostics: [{ code: "SERVICE_REQUEST_TIMEOUT", summary: "locate timed out" }],
      },
    }),
  })).output)
  if (locateRetryPayload.status !== "retryable_failed" || locateRetryPayload.nextAction !== "preview") {
    previewErrors.push(`locate retry state=${locateRetryPayload.status}/${locateRetryPayload.nextAction}`)
  }
  if (
    locateRetryPayload.nextRequest?.tool !== "idea_refactor_preview"
    || locateRetryPayload.nextRequest?.args?.proposalId !== undefined
    || locateRetryPayload.nextRequest?.args?.target?.fqcn !== "demo.Foo"
  ) {
    previewErrors.push(`locate retry nextRequest=${JSON.stringify(locateRetryPayload.nextRequest)}`)
  }

  const prepareRetryPayload = JSON.parse((await hooks.runIdeaPreviewProtocol({
    worktree: "/p",
    cli: "idea-refactor",
    request: {
      projectRoot: "/p",
      operation: "rename:method",
      proposalId: "draft-retry",
      arguments: { newName: "execute" },
    },
    runner: async (_worktree, _cli, argv) => ({
      exitCode: 3,
      stdout: "",
      stderr: "",
      argv,
      json: {
        status: "retryable_failed",
        draftId: "draft-retry",
        operation: "rename:method",
        diagnostics: [{ code: "INDEX_NOT_READY", summary: "prepare must wait" }],
      },
    }),
  })).output)
  if (prepareRetryPayload.status !== "retryable_failed" || prepareRetryPayload.nextAction !== "preview") {
    previewErrors.push(`prepare retry state=${prepareRetryPayload.status}/${prepareRetryPayload.nextAction}`)
  }
  if (
    prepareRetryPayload.nextRequest?.tool !== "idea_refactor_preview"
    || prepareRetryPayload.nextRequest?.args?.proposalId !== "draft-retry"
    || prepareRetryPayload.nextRequest?.args?.target !== undefined
    || prepareRetryPayload.nextRequest?.args?.file !== undefined
  ) {
    previewErrors.push(`prepare retry nextRequest=${JSON.stringify(prepareRetryPayload.nextRequest)}`)
  }

  const decisionPayload = JSON.parse((await hooks.runIdeaPreviewProtocol({
    worktree: "/p",
    cli: "idea-refactor",
    request: {
      projectRoot: "/p",
      operation: "extract:method",
      proposalId: "draft-decision",
      arguments: { newName: "extractPart" },
    },
    runner: async (_worktree, _cli, argv) => ({
      exitCode: 0,
      stdout: "",
      stderr: "",
      argv,
      json: {
        status: "needs_decision",
        draftId: "draft-decision",
        operation: "extract:method",
        decisions: [{
          id: "selection.extract-method.scope",
          kind: "selection",
          recommended: "selection_0",
          choices: [{ value: "selection_0", label: "Use the selected block" }],
        }],
        nextCliCommandExample: {
          action: "prepare",
          argumentsJson: { newName: "extractPart" },
          decisionsJson: {
            "selection.extract-method.scope": { choice: "selection_0", arguments: {} },
          },
        },
        diagnostics: [],
      },
    }),
  })).output)
  if (decisionPayload.status !== "needs_decision" || decisionPayload.nextAction !== "preview") {
    previewErrors.push(`decision state=${decisionPayload.status}/${decisionPayload.nextAction}`)
  }
  if (JSON.stringify(decisionPayload.nextRequest) !== JSON.stringify({
    tool: "idea_refactor_preview",
    args: {
      ideaProjectRoot: "/p",
      operation: "extract:method",
      proposalId: "draft-decision",
      arguments: { newName: "extractPart" },
      decisions: {
        "selection.extract-method.scope": { choice: "selection_0", arguments: {} },
      },
    },
  })) {
    previewErrors.push(`decision nextRequest=${JSON.stringify(decisionPayload.nextRequest)}`)
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
          resolvedContext: {
            stableTargetId: "method:Foo#run",
            filePath: "/p/src/Foo.java",
            kind: "method",
          },
          availableOperations: [],
          operationCandidates: [{
            operation: "extract:method",
            candidates: [{
              "selection-start-line": 4,
              "selection-start-column": 5,
              "selection-end-line": 8,
              "selection-end-column": 6,
            }],
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
  const selectionNextRequest = selectionPayload.selectionCandidates?.[0]?.nextRequest
  if (selectionNextRequest?.tool !== "idea_refactor_preview") {
    previewErrors.push(`selection nextRequest tool=${selectionNextRequest?.tool}`)
  }
  if (selectionNextRequest?.args?.proposalId !== undefined) {
    previewErrors.push("selection nextRequest reused a proposalId")
  }
  if (JSON.stringify(selectionNextRequest?.args?.selection) !== JSON.stringify({
    startLine: 4,
    startColumn: 5,
    endLine: 8,
    endColumn: 6,
  })) {
    previewErrors.push(`selection nextRequest range=${JSON.stringify(selectionNextRequest?.args?.selection)}`)
  }
  if (
    selectionNextRequest?.args?.file !== "/p/src/Foo.java"
    || selectionNextRequest?.args?.line !== 4
    || selectionNextRequest?.args?.column !== 5
  ) {
    previewErrors.push(`selection nextRequest target=${JSON.stringify(selectionNextRequest?.args)}`)
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
  const applyTimeoutPayload = JSON.parse(hooks.renderIdeaApplyProtocolResult(
    "draft-timeout",
    {
      exitCode: 3,
      stdout: "",
      stderr: "",
      argv: ["apply"],
      json: {
        status: "retryable_failed",
        applied: false,
        operation: "rename:method",
        diagnostics: [{ code: "SERVICE_REQUEST_TIMEOUT", summary: "apply response timed out" }],
        nextCliCommandExample: {
          action: "apply",
          argumentsJson: { newName: "execute" },
          decisionsJson: {},
        },
      },
    },
    "compact",
    30000,
    { project_root: "/p" },
  ).output)
  if (applyTimeoutPayload.status !== "outcome_unknown" || applyTimeoutPayload.nextAction !== "verify") {
    previewErrors.push(`apply timeout state=${applyTimeoutPayload.status}/${applyTimeoutPayload.nextAction}`)
  }
  if (applyTimeoutPayload.nextRequest !== undefined) {
    previewErrors.push(`apply timeout suggested a duplicate request=${JSON.stringify(applyTimeoutPayload.nextRequest)}`)
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
    locateRetryStatus: locateRetryPayload.status,
    prepareRetryStatus: prepareRetryPayload.status,
    decisionStatus: decisionPayload.status,
    applyStatus: applyPayload.status,
    applyTimeoutStatus: applyTimeoutPayload.status,
    staleStatus: stalePayload.status,
    decisionSchema: "strict-object",
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
    "makeTaskKey",
    "buildContinuationMessage",
    "buildVerifyRequiredMessage",
    "createIdleContinueRuntime",
    "shouldPluginHandleSessionIdle",
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
  const confirmationInstruction = "Do not edit the candidate; call smell_verify again for one fresh confirmation."
  const confirmationMessage = hooks.buildContinuationMessage({
    continuation: 2,
    maxSmellVerifyCycles: 2,
    instruction: confirmationInstruction,
  })
  assertCond(
    "continuation_instruction_is_only_action_authority",
    confirmationMessage.includes(confirmationInstruction)
      && !confirmationMessage.includes("After one narrow corrective edit")
      && confirmationMessage.indexOf(confirmationInstruction) === confirmationMessage.lastIndexOf(confirmationInstruction),
    confirmationMessage,
  )

  function outputWithLoop({ decision = "continue", continuation = 1, max = 2, status = "SMELL_GUARD_FAILED", nextAction = "", generation } = {}) {
    const payload = JSON.parse(makeFailureOutput(status, status, { next_action: nextAction }))
    payload.loop = {
      generation,
      decision,
      continuation,
      max_smell_verify_cycles: max,
      instruction: decision === "continue" ? "repair from the latest evidence" : "",
    }
    return JSON.stringify(payload)
  }

  const runtimeGenerations = new WeakMap()
  function record(rt, options = {}) {
    const sessionID = options.sessionID || "s1"
    let generations = runtimeGenerations.get(rt)
    if (!generations) {
      generations = new Map()
      runtimeGenerations.set(rt, generations)
    }
    const generation = options.generation ?? ((generations.get(sessionID) || 0) + 1)
    generations.set(sessionID, generation)
    return rt.recordFromBridgeOutput({
      sessionID,
      agent: options.agent || "any-refactor-agent",
      directory: options.directory || IDLE_DIR,
      taskKey: options.taskKey || IDLE_TASK,
      output: options.output || outputWithLoop({ ...options, generation }),
    })
  }

  assertEqual(
    "idle_owner_interactive_default",
    hooks.shouldPluginHandleSessionIdle({}),
    true,
    "enabled",
  )
  assertEqual(
    "idle_owner_batch_exact_marker",
    hooks.shouldPluginHandleSessionIdle({ SMELL_BATCH_RUN: "1" }),
    false,
    "enabled",
  )
  assertEqual(
    "idle_owner_batch_trimmed_marker",
    hooks.shouldPluginHandleSessionIdle({ SMELL_BATCH_RUN: " 1 " }),
    false,
    "enabled",
  )
  assertEqual(
    "idle_owner_nonbatch_marker",
    hooks.shouldPluginHandleSessionIdle({ SMELL_BATCH_RUN: "0" }),
    true,
    "enabled",
  )

  // Interactive mode keeps plugin-owned idle prompts. Dataset batch runs expose
  // the same loop decision but leave prompt transport to the synchronous runner.
  for (const modeCase of [
    { name: "interactive", enabled: true },
    {
      name: "batch",
      env: { SMELL_BATCH_RUN: "1", SMELL_PROJECT_ROOT: "/tmp/project" },
      enabled: false,
    },
  ]) {
    const { client, calls } = makeFakeClient()
    const rt = hooks.createIdleContinueRuntime({ client, env: modeCase.env || {} })
    const metadata = record(rt)
    assertEqual(`idle_owner_${modeCase.name}_enabled`, metadata.enabled, modeCase.enabled, "enabled")
    assertEqual(`unified_${modeCase.name}_continuation`, metadata.continuation, 1, "continuation")
    assertEqual(`unified_${modeCase.name}_max`, metadata.maxSmellVerifyCycles, 2, "maxSmellVerifyCycles")
    assertEqual(`idle_owner_${modeCase.name}_dispatch`, rt.handleIdle("s1"), modeCase.enabled, "dispatch")
    await flush()
    assertEqual(`idle_owner_${modeCase.name}_calls`, calls.length, modeCase.enabled ? 1 : 0, "calls")
    if (!modeCase.enabled) {
      assertEqual(`idle_owner_${modeCase.name}_state`, rt.size(), 0, "state size")
    }
  }

  {
    const { client, calls } = makeFakeClient()
    const rt = hooks.createIdleContinueRuntime({ client, env: { SMELL_BATCH_RUN: "1" } })
    rt.armInitialVerification({
      sessionID: "batch-initial",
      agent: "java-refactor-agent",
      directory: IDLE_DIR,
      maxSmellVerifyCycles: 2,
      instruction: "repair narrowly",
      allowTestChanges: false,
    })
    assertEqual("idle_owner_batch_initial_state", rt.size(), 0, "state size")
    assertEqual("idle_owner_batch_initial_dispatch", rt.handleIdle("batch-initial"), false, "dispatch")
    await flush()
    assertEqual("idle_owner_batch_initial_calls", calls.length, 0, "calls")
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

  // Mutable repair details stay in the tool result rather than being copied
  // into a second, potentially stale user message.
  {
    const { client, calls } = makeFakeClient()
    const rt = hooks.createIdleContinueRuntime({ client })
    const required = "extract cohesive blocks totaling at least 15 AST-NCSS from the frozen method"
    record(rt, { sessionID: "precise-next", nextAction: required })
    rt.handleIdle("precise-next")
    await flush()
    assertCond(
      "unified_latest_tool_result_is_single_source",
      !calls[0].body.parts[0].text.includes(required)
        && calls[0].body.parts[0].text.includes("repair from the latest evidence"),
      "continuation prompt duplicated mutable next-action evidence",
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
      maxSmellVerifyCycles: 2,
      instruction: "repair narrowly",
    })
    assertEqual("verify_required_initial_dispatch", rt.handleIdle("initial"), true, "dispatch")
    await flush()
    assertEqual("verify_required_initial_calls", calls.length, 1, "calls")
    assertCond(
      "verify_required_initial_text",
      calls[0].body.parts[0].text.includes("call smell_verify now")
        && calls[0].body.parts[0].text.includes("verify-required/initial"),
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
      calls[1].body.parts[0].text.includes("call smell_verify now")
        && calls[1].body.parts[0].text.includes("verify-required/continuation"),
      "continuation reminder text missing",
    )
    assertEqual("verify_required_after_continuation_once", rt.handleIdle("after-continuation"), false, "dispatch")
    record(rt, { sessionID: "after-continuation", output: makePassOutput() })
    assertEqual("verify_required_cleared_by_verify", rt.handleIdle("after-continuation"), false, "dispatch")
  }

  return {
    ownershipCases: ["interactive", "batch"],
    batchIdleController: "runner",
    sharedBudget: true,
    verifyClosure: true,
    passed: true,
  }
}

function runCommandPolicyDecisionSelfCheck(pluginModule) {
  const hooks = pluginModule.SmellPlugin?.__selfTest || pluginModule.default?.__selfTest
  assertCond("command_decision_hook", typeof hooks?.applyCommandLoopDecision === "function", "missing applyCommandLoopDecision")
  assertCond("guard_progress_decision_hook", typeof hooks?.applyGuardProgressDecision === "function", "missing applyGuardProgressDecision")
  assertCond("guard_progress_observation_hook", typeof hooks?.guardProgressObservation === "function", "missing guardProgressObservation")
  assertCond(
    "formal_candidate_consistency_hook",
    typeof hooks?.applyFormalVerificationConsistency === "function",
    "missing applyFormalVerificationConsistency",
  )
  assertCond("command_state_snapshot_hook", typeof hooks?.commandLoopStateSnapshot === "function", "missing commandLoopStateSnapshot")
  assertCond("command_state_restore_hook", typeof hooks?.restoreCommandLoopState === "function", "missing restoreCommandLoopState")
  assertEqual(
    "java_checkpoint_identity_detected",
    hooks.isJavaCheckpointIdentity({ checkpointRequired: true, language: "java", smell: "long_method", location: "Foo.java:1" }),
    true,
    "identity",
  )
  assertEqual(
    "non_java_identity_unchanged",
    hooks.isJavaCheckpointIdentity({ checkpointRequired: true, language: "python", smell: "long_method", location: "foo.py:1" }),
    false,
    "identity",
  )
  assertEqual(
    "bridge_checkpoint_requirement_is_authoritative",
    hooks.isJavaCheckpointIdentity({ checkpointRequired: false, language: "java", smell: "long_method", location: "Foo.java:1" }),
    false,
    "identity",
  )
  assertCond(
    "java_checkpoint_uses_cheap_guard_progress_gate",
    typeof hooks.usesCheapGuardProgressGate === "function"
      && hooks.usesCheapGuardProgressGate(
        { language: "java" },
        {
          policy: {
            checkpoint_required: true,
            identity: { language: "java" },
          },
        },
      ),
    "Java checkpoint did not enter the source-only Guard progress gate",
  )
  assertCond(
    "java_location_uses_cheap_guard_progress_gate",
    hooks.usesCheapGuardProgressGate(
      { language: "", location: "Foo.java:1" },
      {
        policy: {
          checkpoint_required: true,
          identity: { language: "", location: "Foo.java:1" },
        },
      },
    ),
    "Java checkpoint location did not enter the source-only Guard progress gate",
  )
  const state = {
    policy: {
      task: "task",
      verification_mode: "project_full",
      refactoring_backend: "direct",
      allow_test_changes: false,
      checkpoint_required: true,
      identity: {
        project_root: "/tmp/project",
        project_override_root: "",
        language: "java",
        smell: "long_method",
        location: "Foo.java:1",
        target_context_json: "",
        verification_mode: "project_full",
        sample_test_location: "",
        sample_test_command: "",
        build_command: "",
        project_test_command: "",
        verification_cwd: "",
        verification_command_source: "",
        sample_test_source: "",
      },
      loop: {
        mode: "verify-failure",
        max_smell_verify_cycles: 2,
        no_progress_limit: 3,
        allowed_failure_groups: ["smell"],
        instruction: "repair narrowly",
        sample_deadline_seconds: 1800,
      },
    },
    targetIdentityContext: "",
    startedAt: Date.now(),
    control: {
      generation: 0,
      decision: "verify_required",
      instruction: "Call smell_verify now using the frozen command identity.",
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
    ideaProtocolState: hooks.newIdeaProtocolState(),
    terminalReceipt: null,
  }
  const serializedState = hooks.commandLoopStateSnapshot(state)
  assertEqual("command_state_schema_v7", serializedState.schema_version, 7, "schema_version")
  assertEqual(
    "command_state_v7_formal_candidate_initial",
    serializedState.formal_candidate_state.confirmation_required,
    false,
    "formal_candidate_state.confirmation_required",
  )
  assertEqual(
    "command_state_v5_rejected_without_fallback",
    hooks.restoreCommandLoopState(JSON.stringify({ ...serializedState, schema_version: 5 })),
    undefined,
    "restored state",
  )
  const missingV6Field = { ...serializedState }
  delete missingV6Field.control
  assertEqual(
    "command_state_v7_missing_field_rejected",
    hooks.restoreCommandLoopState(JSON.stringify(missingV6Field)),
    undefined,
    "restored state",
  )
  const missingFormalCandidateState = { ...serializedState }
  delete missingFormalCandidateState.formal_candidate_state
  assertEqual(
    "command_state_v7_missing_formal_candidate_rejected",
    hooks.restoreCommandLoopState(JSON.stringify(missingFormalCandidateState)),
    undefined,
    "restored state",
  )
  assertCond(
    "command_state_valid_snapshot_restores",
    Boolean(hooks.restoreCommandLoopState(JSON.stringify(serializedState))),
    "valid state did not restore",
  )
  assertEqual("command_state_has_no_hidden_cap_recovery", "cap_recovery_used" in serializedState, false, "cap_recovery_used")
  assertEqual(
    "command_state_invalid_fingerprint_rejected",
    hooks.restoreCommandLoopState(JSON.stringify({ ...serializedState, last_failure_fingerprint: 7 })),
    undefined,
    "last_failure_fingerprint",
  )
  const candidateIdentity = (
    productionDiff = "candidate-diff-a",
    testTree = "test-tree-a",
    verificationConfigTree = "verification-config-tree-a",
  ) => ({
    baseline_revision: "baseline-revision",
    baseline_tree: "baseline-tree",
    production_diff: productionDiff,
    test_tree: testTree,
    verification_config_tree: verificationConfigTree,
  })
  const formalReceipt = ({
    status,
    success,
    accepted,
    resolution,
    outcome,
    diagnosticSignature,
    productionDiff = "candidate-diff-a",
    testTree = "test-tree-a",
    verificationConfigTree = "verification-config-tree-a",
  }) => ({
    schema_version: "smell.formal-verification-receipt/v1",
    terminal_stage: "formal_verify",
    status,
    success,
    accepted,
    resolution,
    candidate_identity: candidateIdentity(productionDiff, testTree, verificationConfigTree),
    outcome,
    diagnostic_signature: diagnosticSignature,
    guard: {
      success: true,
      failure_count: 0,
      artifact_ref: "/tmp/artifacts/guard-evidence.json",
    },
    build_test: {
      success,
      reason: success ? "" : "TEST_FAILED",
      project_full_executed: true,
      build_status: "passed",
      test_status: success ? "passed" : "failed",
      sample_test_status: "",
    },
    fresh_isolation: {
      contract_version: "project-full-fresh-worktree/v1",
      mode: "detached_git_worktree",
      success: true,
      stage: "completed",
      cleanup_success: true,
    },
    artifact_refs: {
      guard_evidence: "/tmp/artifacts/guard-evidence.json",
      test_result: "/tmp/artifacts/test.full.json",
    },
  })
  const formalFailure = (diagnosticSignature, productionDiff = "candidate-diff-a") => ({
    success: false,
    accepted: false,
    progress: false,
    status: "TEST_FAILED",
    resolution: "unresolved",
    failure_fingerprint: diagnosticSignature,
    failure_pack: {
      failure_category: "TEST_BEHAVIOR_REGRESSION",
      failure_group: "test",
      retryable: true,
      verify_status: "TEST_FAILED",
      next_action: "repair the behavior regression",
    },
    checkpoint: { delta: { has_production_diff: true } },
    formal_verification_receipt: formalReceipt({
      status: "TEST_FAILED",
      success: false,
      accepted: false,
      resolution: "unresolved",
      outcome: "test_failed",
      diagnosticSignature,
      productionDiff,
    }),
  })
  const formalPass = (
    productionDiff = "candidate-diff-a",
    testTree = "test-tree-a",
    verificationConfigTree = "verification-config-tree-a",
  ) => ({
    success: true,
    accepted: true,
    progress: true,
    project_full_executed: true,
    status: "PASS",
    resolution: "resolved",
    formal_verification_receipt: formalReceipt({
      status: "PASS",
      success: true,
      accepted: true,
      resolution: "resolved",
      outcome: "pass",
      diagnosticSignature: "PASS",
      productionDiff,
      testTree,
      verificationConfigTree,
    }),
  })
  const newFormalState = () => ({
    ...state,
    policy: {
      ...state.policy,
      loop: {
        ...state.policy.loop,
        max_smell_verify_cycles: 2,
        no_progress_limit: 3,
        allowed_failure_groups: ["smell", "compile", "test"],
      },
    },
    startedAt: Date.now(),
    control: { ...state.control },
    smellVerifyCycleCount: 0,
    noProgressCount: 0,
    lastFailureFingerprint: "",
    formalCandidateState: {
      candidateIdentity: null,
      outcome: "",
      diagnosticSignature: "",
      confirmationRequired: false,
    },
    terminalReceipt: null,
  })

  const flakyState = newFormalState()
  const failedFormal = { output: JSON.stringify(formalFailure("test-diagnostic-a")), metadata: {} }
  hooks.applyCommandLoopDecision(failedFormal, flakyState)
  assertEqual("formal_first_test_failure_continues", JSON.parse(failedFormal.output).loop.decision, "continue", "decision")
  const restoredBeforeContradiction = hooks.restoreCommandLoopState(
    JSON.stringify(hooks.commandLoopStateSnapshot(flakyState)),
  )
  assertCond("formal_candidate_state_survives_restart", Boolean(restoredBeforeContradiction), "state did not restore")
  const contradictoryPass = { output: JSON.stringify(formalPass()), metadata: {} }
  hooks.applyCommandLoopDecision(contradictoryPass, restoredBeforeContradiction)
  const contradictoryPayload = JSON.parse(contradictoryPass.output)
  assertEqual("formal_same_candidate_pass_is_flaky", contradictoryPayload.status, "FLAKY_TEST_INCONCLUSIVE", "status")
  assertEqual("formal_same_candidate_pass_not_accepted", contradictoryPayload.accepted, false, "accepted")
  assertEqual("formal_same_candidate_requires_confirmation", contradictoryPayload.loop.decision, "continue", "decision")
  assertEqual(
    "formal_confirmation_instruction_forbids_edit",
    contradictoryPayload.loop.instruction,
    "Do not edit the candidate; call smell_verify again for one fresh confirmation.",
    "instruction",
  )
  assertEqual("formal_same_candidate_not_terminal", restoredBeforeContradiction.terminalReceipt, null, "terminalReceipt")
  assertEqual(
    "formal_confirmation_state_latched",
    restoredBeforeContradiction.formalCandidateState.confirmationRequired,
    true,
    "confirmationRequired",
  )
  const exhaustedConfirmationState = newFormalState()
  exhaustedConfirmationState.smellVerifyCycleCount = 2
  exhaustedConfirmationState.formalCandidateState = {
    candidateIdentity: {
      baselineRevision: "baseline-revision",
      baselineTree: "baseline-tree",
      productionDiff: "candidate-diff-a",
      testTree: "test-tree-a",
      verificationConfigTree: "verification-config-tree-a",
    },
    outcome: "test_failed",
    diagnosticSignature: "test-diagnostic-a",
    confirmationRequired: false,
  }
  const exhaustedPass = { output: JSON.stringify(formalPass()), metadata: {} }
  hooks.applyCommandLoopDecision(exhaustedPass, exhaustedConfirmationState)
  const exhaustedPayload = JSON.parse(exhaustedPass.output)
  assertEqual("formal_confirmation_exhausted_status", exhaustedPayload.status, "FLAKY_TEST_INCONCLUSIVE", "status")
  assertEqual("formal_confirmation_exhausted_stops", exhaustedPayload.loop.decision, "stop", "decision")
  assertEqual("formal_confirmation_exhausted_not_accepted", exhaustedPayload.accepted, false, "accepted")
  const restoredForConfirmation = hooks.restoreCommandLoopState(
    JSON.stringify(hooks.commandLoopStateSnapshot(restoredBeforeContradiction)),
  )
  const confirmedPass = { output: JSON.stringify(formalPass()), metadata: {} }
  hooks.applyCommandLoopDecision(confirmedPass, restoredForConfirmation)
  const confirmedPayload = JSON.parse(confirmedPass.output)
  assertEqual("formal_fresh_confirmation_passes", confirmedPayload.status, "PASS", "status")
  assertEqual("formal_fresh_confirmation_terminal", confirmedPayload.loop.termination_reason, "PASS", "termination")
  assertEqual("formal_fresh_confirmation_accepted", confirmedPayload.accepted, true, "accepted")
  assertEqual(
    "formal_terminal_receipt_keeps_evidence",
    restoredForConfirmation.terminalReceipt.formalVerificationReceipt.artifact_refs.test_result,
    "/tmp/artifacts/test.full.json",
    "artifact ref",
  )
  const restoredTerminal = hooks.restoreCommandLoopState(
    JSON.stringify(hooks.commandLoopStateSnapshot(restoredForConfirmation)),
  )
  assertEqual(
    "formal_terminal_receipt_survives_restart",
    restoredTerminal.terminalReceipt.formalVerificationReceipt.fresh_isolation.cleanup_success,
    true,
    "cleanup_success",
  )

  const diagnosticState = newFormalState()
  hooks.applyCommandLoopDecision(
    { output: JSON.stringify(formalFailure("test-diagnostic-a")), metadata: {} },
    diagnosticState,
  )
  const changedDiagnostic = {
    output: JSON.stringify(formalFailure("test-diagnostic-b")),
    metadata: {},
  }
  hooks.applyCommandLoopDecision(changedDiagnostic, diagnosticState)
  assertEqual(
    "formal_same_candidate_diagnostic_conflict",
    JSON.parse(changedDiagnostic.output).status,
    "FLAKY_TEST_INCONCLUSIVE",
    "status",
  )

  const changedCandidateState = newFormalState()
  hooks.applyCommandLoopDecision(
    { output: JSON.stringify(formalFailure("test-diagnostic-a")), metadata: {} },
    changedCandidateState,
  )
  const changedCandidatePass = {
    output: JSON.stringify(formalPass("candidate-diff-b")),
    metadata: {},
  }
  hooks.applyCommandLoopDecision(changedCandidatePass, changedCandidateState)
  assertEqual(
    "formal_changed_candidate_passes_without_false_flaky",
    JSON.parse(changedCandidatePass.output).status,
    "PASS",
    "status",
  )

  const changedTestTreeState = newFormalState()
  hooks.applyCommandLoopDecision(
    { output: JSON.stringify(formalFailure("test-diagnostic-a")), metadata: {} },
    changedTestTreeState,
  )
  const changedTestTreePass = {
    output: JSON.stringify(formalPass("candidate-diff-a", "test-tree-b")),
    metadata: {},
  }
  hooks.applyCommandLoopDecision(changedTestTreePass, changedTestTreeState)
  assertEqual(
    "formal_changed_test_tree_is_a_different_candidate",
    JSON.parse(changedTestTreePass.output).status,
    "PASS",
    "status",
  )

  const missingReceiptState = newFormalState()
  const missingReceiptPass = {
    output: JSON.stringify({ success: true, accepted: true, status: "PASS", resolution: "resolved" }),
    metadata: {},
  }
  hooks.applyCommandLoopDecision(missingReceiptPass, missingReceiptState)
  assertEqual(
    "formal_checkpoint_pass_without_receipt_fails_closed",
    JSON.parse(missingReceiptPass.output).status,
    "FORMAL_VERIFICATION_RECEIPT_INVALID",
    "status",
  )
  const incompleteIdentityState = newFormalState()
  const incompleteIdentityPass = formalPass()
  delete incompleteIdentityPass.formal_verification_receipt.candidate_identity.test_tree
  const incompleteIdentityResult = {
    output: JSON.stringify(incompleteIdentityPass),
    metadata: {},
  }
  hooks.applyCommandLoopDecision(incompleteIdentityResult, incompleteIdentityState)
  assertEqual(
    "formal_checkpoint_receipt_missing_test_identity_fails_closed",
    JSON.parse(incompleteIdentityResult.output).status,
    "FORMAL_VERIFICATION_RECEIPT_INVALID",
    "status",
  )
  const emptyJavaIdentityState = newFormalState()
  const emptyJavaIdentityResult = {
    output: JSON.stringify(formalPass("candidate-diff-java", "", "")),
    metadata: {},
  }
  hooks.applyCommandLoopDecision(emptyJavaIdentityResult, emptyJavaIdentityState)
  assertEqual(
    "formal_java_receipt_empty_test_identity_fails_closed",
    JSON.parse(emptyJavaIdentityResult.output).status,
    "FORMAL_VERIFICATION_RECEIPT_INVALID",
    "status",
  )
  const nonJavaIdentityState = {
    ...newFormalState(),
    policy: {
      ...state.policy,
      identity: {
        ...state.policy.identity,
        language: "python",
        location: "sample.py:1",
      },
    },
  }
  const emptyNonJavaIdentityResult = {
    output: JSON.stringify(formalPass("candidate-diff-python", "", "")),
    metadata: {},
  }
  hooks.applyCommandLoopDecision(emptyNonJavaIdentityResult, nonJavaIdentityState)
  assertEqual(
    "formal_nonjava_receipt_allows_empty_java_trees",
    JSON.parse(emptyNonJavaIdentityResult.output).status,
    "PASS",
    "status",
  )
  assertCond(
    "formal_nonjava_empty_tree_state_restores",
    Boolean(hooks.restoreCommandLoopState(JSON.stringify(hooks.commandLoopStateSnapshot(nonJavaIdentityState)))),
    "non-Java formal state with empty Java-only trees did not restore",
  )
  const incompleteProjectFullState = newFormalState()
  const incompleteProjectFullPass = formalPass()
  incompleteProjectFullPass.formal_verification_receipt.build_test.project_full_executed = false
  const incompleteProjectFullResult = {
    output: JSON.stringify(incompleteProjectFullPass),
    metadata: {},
  }
  hooks.applyCommandLoopDecision(incompleteProjectFullResult, incompleteProjectFullState)
  assertEqual(
    "formal_project_full_pass_requires_executed_receipt",
    JSON.parse(incompleteProjectFullResult.output).status,
    "FORMAL_VERIFICATION_RECEIPT_INVALID",
    "status",
  )
  const guardState = {
    ...state,
    policy: {
      ...state.policy,
      loop: {
        ...state.policy.loop,
        no_progress_limit: 5,
      },
    },
    smellVerifyCycleCount: 0,
    noProgressCount: 0,
    lastFailureFingerprint: "",
    bestMetricDeficit: null,
    bestStructuralFailureCount: null,
    lastBlockerCodes: [],
    seenStructuralStates: [],
    control: { ...state.control },
    terminalReceipt: null,
  }
  const guardRequired = {
    output: JSON.stringify({
      schema_version: "smell.guard-progress/v1",
      success: false,
      status: "GUARD_PROGRESS_REQUIRED",
      metric_budget: [{ required_reduction: 3 }],
      source_guard_feedback: {
        progress_observation: { metric_deficit: 3, structural_failure_count: 1 },
      },
      next_action: "repair the structural contract",
    }),
    metadata: {},
  }
  hooks.applyGuardProgressDecision(guardRequired, guardState)
  const guardPayload = JSON.parse(guardRequired.output)
  assertEqual("guard_progress_consumes_shared_budget", guardPayload.loop.continuation, 1, "continuation")
  assertEqual("guard_progress_arms_manual_continue", guardPayload.loop.decision, "continue", "decision")
  const guardRepeated = { output: guardRequired.output.replace('"loop":', '"prior_loop":'), metadata: {} }
  hooks.applyGuardProgressDecision(guardRepeated, guardState)
  const repeatedPayload = JSON.parse(guardRepeated.output)
  assertEqual("guard_progress_no_progress_uses_loop_budget", repeatedPayload.loop.decision, "continue", "decision")
  assertEqual("guard_progress_no_progress_not_terminal", Boolean(guardState.terminalReceipt), false, "terminalReceipt")
  const guardExhausted = { output: guardRepeated.output.replace('"loop":', '"prior_loop":'), metadata: {} }
  hooks.applyGuardProgressDecision(guardExhausted, guardState)
  const guardExhaustedPayload = JSON.parse(guardExhausted.output)
  assertEqual("guard_progress_shared_budget_terminal", guardExhaustedPayload.loop.decision, "stop", "decision")
  assertEqual("guard_progress_shared_budget_reason", guardExhaustedPayload.loop.termination_reason, "MAX_SMELL_VERIFY_CYCLES_REACHED", "termination")
  assertEqual("guard_progress_terminal_latched", Boolean(guardState.terminalReceipt), true, "terminalReceipt")
  const phaseState = {
    ...state,
    policy: {
      ...state.policy,
      loop: {
        ...state.policy.loop,
        max_smell_verify_cycles: 5,
        no_progress_limit: 5,
      },
    },
    control: { ...state.control },
    smellVerifyCycleCount: 0,
    noProgressCount: 0,
    lastFailureFingerprint: "",
    bestMetricDeficit: null,
    bestStructuralFailureCount: null,
    lastBlockerCodes: [],
    seenStructuralStates: [],
    terminalReceipt: null,
  }
  const observePhase = (metric, structural, blockerCodes) => {
    const result = {
      output: JSON.stringify({
        schema_version: "smell.guard-progress/v1",
        success: false,
        status: "GUARD_PROGRESS_REQUIRED",
        source_guard_feedback: {
          next_action: "repair the current blocker",
          progress_observation: {
            metric_deficit: metric,
            structural_failure_count: structural,
            blocker_codes: blockerCodes,
          },
        },
      }),
      metadata: {},
    }
    hooks.applyGuardProgressDecision(result, phaseState)
    return JSON.parse(result.output).progress_observation.strictly_improved
  }
  assertEqual("guard_phase_initial_structural", observePhase(5, 2, ["A"]), true, "progress")
  assertEqual("guard_phase_equal_new_blocker_once", observePhase(4, 2, ["B"]), true, "progress")
  assertEqual("guard_phase_cycle_not_progress", observePhase(3, 2, ["A"]), false, "progress")
  assertEqual("guard_phase_structural_clear_wins_metric_rise", observePhase(9, 0, []), true, "progress")
  assertEqual("guard_phase_new_structure_never_progress", observePhase(1, 1, ["C"]), false, "progress")
  assertEqual("guard_phase_scalar_decline_after_clear", observePhase(8, 0, []), true, "progress")
  const restoredPhaseState = hooks.restoreCommandLoopState(
    JSON.stringify(hooks.commandLoopStateSnapshot(phaseState)),
  )
  assertCond("guard_phase_state_restores", Boolean(restoredPhaseState), "phase state did not restore")
  assertCond(
    "guard_phase_seen_states_persist",
    restoredPhaseState.seenStructuralStates.includes("2:A")
      && restoredPhaseState.seenStructuralStates.includes("2:B")
      && restoredPhaseState.seenStructuralStates.includes("1:C"),
    "seen structural states did not persist",
  )
  const revisionState = {
    ...phaseState,
    policy: {
      ...phaseState.policy,
      loop: {
        ...phaseState.policy.loop,
        no_progress_limit: 1,
      },
    },
    control: { ...state.control },
    smellVerifyCycleCount: 0,
    noProgressCount: 0,
    lastFailureFingerprint: "",
    bestMetricDeficit: null,
    bestStructuralFailureCount: null,
    lastBlockerCodes: [],
    seenStructuralStates: [],
    terminalReceipt: null,
  }
  const observeRevision = (candidateRevision) => {
    const result = {
      output: JSON.stringify({
        schema_version: "smell.guard-progress/v1",
        success: false,
        status: "GUARD_PROGRESS_REQUIRED",
        source_guard_feedback: {
          next_action: "repair the current blocker",
          progress_observation: {
            metric_deficit: 3,
            structural_failure_count: 1,
            blocker_codes: ["A"],
            candidate_revision: candidateRevision,
          },
        },
      }),
      metadata: {},
    }
    hooks.applyGuardProgressDecision(result, revisionState)
    return JSON.parse(result.output)
  }
  assertEqual("guard_revision_first_continues", observeRevision("revision-1").loop.decision, "continue", "decision")
  assertEqual("guard_revision_change_resets_stall", observeRevision("revision-2").loop.decision, "continue", "decision")
  const unchangedRevision = observeRevision("revision-2")
  assertEqual("guard_revision_unchanged_stops", unchangedRevision.loop.decision, "stop", "decision")
  assertEqual("guard_revision_unchanged_reason", unchangedRevision.loop.termination_reason, "NO_PROGRESS_LIMIT_REACHED", "termination")
  const fallbackObservation = hooks.guardProgressObservation({
    guard_failure_count: 2,
    source_guard_feedback: {
      metric_budget: [{ required_reduction: 4 }, { required_reduction: 3 }],
      blocker: { kind: "declaration_identity" },
    },
  })
  assertEqual("guard_progress_current_feedback_metric_fallback", fallbackObservation.metricDeficit, 7, "metricDeficit")
  assertEqual("guard_progress_current_feedback_structural_fallback", fallbackObservation.structuralFailureCount, 2, "structuralFailureCount")
  assertEqual(
    "guard_progress_blocker_structural_fallback",
    hooks.guardProgressObservation({ source_guard_feedback: { blocker: { kind: "declaration_identity" } } }).structuralFailureCount,
    1,
    "structuralFailureCount",
  )
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
  const routeLockedPrompt = hooks.commandControllerSystemContext({
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
    !routeLockedPrompt.includes("Smell evidence: parents=Packet")
      && !routeLockedPrompt.includes("Mandatory Refused Bequest route lock:")
      && routeLockedPrompt.includes("Baseline capture must uniquely confirm the requested smell at the supplied target")
      && routeLockedPrompt.includes("context selects the entity but never supplies a verdict")
      && routeLockedPrompt.includes("substantive production-source refactoring")
      && !routeLockedPrompt.includes("production-Java"),
    "dataset route metadata entered the command contract",
  )
  const sampleOptimizedPrompt = hooks.commandControllerSystemContext({
    ...state.policy,
    verification_mode: "sample_optimized",
  })
  assertCond(
    "command_prompt_uses_frozen_verification_mode",
    sampleOptimizedPrompt.includes("verification_mode: sample_optimized")
      && sampleOptimizedPrompt.includes("controller-owned staged gate under verification_mode=sample_optimized")
      && !sampleOptimizedPrompt.includes("smell_verify(project_full)"),
    "controller prompt hardcoded project_full instead of the frozen verification mode",
  )
  const protectedCandidatePrompt = hooks.commandControllerSystemContext({
    ...state.policy,
    identity: { ...state.policy.identity, language: "cpp" },
  })
  assertCond(
    "command_prompt_protects_project_full_nonjava_candidate_tree",
    protectedCandidatePrompt.includes("Bash is disabled for this controller-managed project_full Python/C/C++ session")
      && protectedCandidatePrompt.includes("Call smell_verify for every compile or test"),
    "controller prompt omitted the protected candidate source-tree contract",
  )
  const javaCandidatePrompt = hooks.commandControllerSystemContext(state.policy)
  assertCond(
    "command_prompt_keeps_java_shell_policy",
    !javaCandidatePrompt.includes("Bash is disabled for this controller-managed project_full Python/C/C++ session"),
    "controller prompt applied the non-Java candidate-shell policy to Java",
  )
  assertCond(
    "command_prompt_keeps_non_project_full_shell_policy",
    !sampleOptimizedPrompt.includes("Bash is disabled for this controller-managed project_full Python/C/C++ session"),
    "controller prompt applied the candidate-shell policy outside project_full",
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
      files: ["src/Leaked.java"],
      callers: ["leakedCaller()"],
      forbidden: ["move the finding to another method in the same source owner"],
      metric_budget: [
        {
          metric: "envy_access_count",
          current: 9,
          passing_exclusive_max: 5,
          required_reduction: 5,
          unit: "foreign accesses",
          file: "src/Leaked.java",
          caller: "leakedCaller()",
          next_action: "leaked budget action",
        },
      ],
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
    "checkpoint_target_identity_has_bounded_metric_budget",
    featureTargetIdentityPrompt.includes("metric=envy_access_count")
      && featureTargetIdentityPrompt.includes("current=9")
      && featureTargetIdentityPrompt.includes("passing_exclusive_max=5")
      && featureTargetIdentityPrompt.includes("required_reduction=5")
      && featureTargetIdentityPrompt.includes("unit=foreign accesses")
      && featureTargetIdentityPrompt.includes("necessary planning information, not acceptance authority")
      && featureTargetIdentityPrompt.includes("performs only the source check")
      && featureTargetIdentityPrompt.includes("cannot accept the sample or execute project_full")
      && featureTargetIdentityPrompt.includes("Do not manually run a heavy project build in the candidate source tree")
      && featureTargetIdentityPrompt.includes("same smell_verify call advances to final acceptance")
      && !featureTargetIdentityPrompt.includes("every budget boundary")
      && !featureTargetIdentityPrompt.includes("sole smell_verify"),
    "feature-envy identity prompt did not render the bounded planning budget",
  )
  assertCond(
    "checkpoint_target_identity_budget_omits_closure_lists",
    !featureTargetIdentityPrompt.includes("src/Leaked.java")
      && !featureTargetIdentityPrompt.includes("leakedCaller()")
      && !featureTargetIdentityPrompt.includes("leaked budget action")
      && !featureTargetIdentityPrompt.includes("close the complete document receiver collaboration")
      && !featureTargetIdentityPrompt.includes("envy_access_diff"),
    "feature-envy baseline prompt leaked mutable closure or raw metric fields",
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
      && dataClumpsContractPrompt.includes("retain the old parameter group in a wrapper")
      && !dataClumpsContractPrompt.includes("remove every old-group wrapper")
      && dataClumpsContractPrompt.includes("latest smell_verify tool result"),
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
  assertEqual("command_state_count_survives_restart", restoredAfterRestart.smellVerifyCycleCount, 1, "smellVerifyCycleCount")
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
    "",
    "termination",
  )
  const second = { output: JSON.stringify(failure), metadata: {} }
  hooks.applyCommandLoopDecision(second, state)
  const secondPayload = JSON.parse(second.output)
  assertEqual("command_decision_no_progress_uses_loop_budget", secondPayload.loop.decision, "continue", "decision")

  // An IMPROVED result keeps the loop running toward
  // resolved (with the bridge continue_hint), and only identical best-partial
  // objectives across verifies count as no-progress.
  const improvedState = {
    ...state,
    startedAt: Date.now(),
    control: { ...state.control },
    smellVerifyCycleCount: 0,
    noProgressCount: 0,
    lastFailureFingerprint: "",
    terminalReceipt: null,
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
    "improved_no_progress_uses_loop_budget",
    improvedSecondPayload.loop.termination_reason,
    "",
    "termination",
  )
  assertEqual("improved_no_progress_decision", improvedSecondPayload.loop.decision, "continue", "decision")

  const resolved = formalPass()
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
      smellVerifyCycleCount: 0,
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
    ...state,
    policy: capPolicy,
    startedAt: Date.now(),
    control: { ...state.control },
    smellVerifyCycleCount: 2,
    noProgressCount: 0,
    lastFailureFingerprint: "",
    terminalReceipt: null,
  }
  const progressingAtCap = {
    ...failure,
    checkpoint: { delta: { metric_progress: true } },
  }
  const exactCap = { output: JSON.stringify(progressingAtCap), metadata: {} }
  hooks.applyCommandLoopDecision(exactCap, capState)
  const exactCapPayload = JSON.parse(exactCap.output)
  assertEqual("cycle_cap_is_exact", exactCapPayload.loop.decision, "stop", "decision")
  assertEqual("cycle_cap_count", exactCapPayload.loop.continuation, 2, "continuation")
  assertEqual("cycle_cap_reason", exactCapPayload.loop.termination_reason, "MAX_SMELL_VERIFY_CYCLES_REACHED", "termination")
  assertEqual("cycle_cap_has_no_hidden_recovery", "cap_recovery_used" in exactCapPayload.loop, false, "cap_recovery_used")
  const restoredCapState = hooks.restoreCommandLoopState(
    JSON.stringify(hooks.commandLoopStateSnapshot(capState)),
  )
  const restartedAtCap = { output: JSON.stringify(progressingAtCap), metadata: {} }
  hooks.applyCommandLoopDecision(restartedAtCap, restoredCapState)
  assertEqual(
    "cycle_cap_survives_restart",
    JSON.parse(restartedAtCap.output).loop.termination_reason,
    "MAX_SMELL_VERIFY_CYCLES_REACHED",
    "termination",
  )

  const compileCapState = {
    ...capState,
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
  assertEqual("compile_checkpoint_diff_stops_at_exact_cap", JSON.parse(compileRecovery.output).loop.decision, "stop", "decision")

  const noProgressCapState = {
    ...capState,
    noProgressCount: 0,
    lastFailureFingerprint: "",
  }
  const noProgressAtCap = {
    ...failure,
    checkpoint: { delta: { metric_progress: false } },
  }
  const noCapRecovery = { output: JSON.stringify(noProgressAtCap), metadata: {} }
  hooks.applyCommandLoopDecision(noCapRecovery, noProgressCapState)
  assertEqual("cap_without_progress_stops", JSON.parse(noCapRecovery.output).loop.termination_reason, "MAX_SMELL_VERIFY_CYCLES_REACHED", "termination")
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
      SMELL_SESSION_STATE_ROOT: path.join(tempRoot, "session-state"),
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
      const systemHook = plugin?.["experimental.chat.system.transform"]
      if (typeof systemHook !== "function") {
        throw new SelfCheckError("controller_system_hook", "experimental.chat.system.transform hook was not registered.", {})
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
          "unowned_session_without_command_state_fails_closed",
          String(error?.message || error).includes("COMMAND_POLICY_STATE_MISSING"),
          String(error?.message || error),
        )
      }
      const commandOutput = { parts: [{ type: "text", text: "placeholder" }] }
      const originalCommandParts = JSON.stringify(commandOutput.parts)
      const controllerAuditFile = path.join(tempRoot, "controller-system.txt")
      process.env.SMELL_CONTROLLER_CONTEXT_AUDIT_FILE = controllerAuditFile
      await commandHook(
        {
          command: "java-refactor-run",
          sessionID: "command-policy-self-check",
          arguments: `--verification-mode=project_full --max-smell-verify-cycles=2 --loop-no-progress-limit=1 -- Project root: ${fixtureRoot}\nSmell type: long_method\nTarget location: src/main/java/SelfCheckSample.java:2\nSample test command: ${sampleTestCommand}`,
        },
        commandOutput,
      )
      assertCond(
        "command_preserves_user_parts",
        JSON.stringify(commandOutput.parts) === originalCommandParts,
        "command hook mutated the command-expanded user message",
      )
      const systemOutput = { system: ["base-system-context"] }
      await systemHook(
        { sessionID: "command-policy-self-check", model: {} },
        systemOutput,
      )
      const controllerContexts = systemOutput.system.filter((item) =>
        item.includes('<smell-controller-context schema="1">')
      )
      assertEqual("controller_system_context_once", controllerContexts.length, 1, "context count")
      assertCond(
        "controller_system_context_separate",
        systemOutput.system[0] === "base-system-context"
          && controllerContexts[0].includes("Controller-owned verification, identity, and loop policy")
          && !controllerContexts[0].includes("Sample test command:"),
        "stable controller context was not appended separately",
      )
      assertCond(
        "controller_system_context_has_first_edit_budget",
        controllerContexts[0].includes("Immutable numeric edit budget")
          && controllerContexts[0].includes("required_reduction=")
          && controllerContexts[0].includes("necessary planning information, not acceptance authority")
          && controllerContexts[0].includes("performs only the source check")
          && controllerContexts[0].includes("cannot accept the sample or execute project_full")
          && controllerContexts[0].includes("Do not manually run a heavy project build in the candidate source tree")
          && controllerContexts[0].includes("same smell_verify call advances to final acceptance")
          && controllerContexts[0].includes("controller-owned staged gate under verification_mode=project_full")
          && !controllerContexts[0].includes("every budget boundary")
          && !controllerContexts[0].includes("sole smell_verify"),
        "first-turn system context did not include the bounded metric budget and verify timing",
      )
      assertCond(
        "controller_system_context_omits_mutable_baseline_closure",
        !controllerContexts[0].includes("worklist_count")
          && !controllerContexts[0].includes("next_action")
          && !controllerContexts[0].includes("src/Leaked.java")
          && !controllerContexts[0].includes("leakedCaller()"),
        "first-turn system context leaked mutable closure details",
      )
      const frozenControllerContext = controllerContexts[0]
      assertEqual(
        "controller_system_context_audited",
        await readFile(controllerAuditFile, "utf8"),
        `${frozenControllerContext}\n`,
        "audit contents",
      )
      await systemHook(
        { sessionID: "command-policy-self-check", model: {} },
        systemOutput,
      )
      assertEqual(
        "controller_system_context_deduplicated",
        systemOutput.system.filter((item) => item === frozenControllerContext).length,
        1,
        "context count",
      )
      const unrelatedSystemOutput = { system: ["base-system-context"] }
      await systemHook({ sessionID: "unowned-system-self-check", model: {} }, unrelatedSystemOutput)
      assertEqual("unowned_system_context_unchanged", unrelatedSystemOutput.system.length, 1, "system count")
      delete process.env.SMELL_CONTROLLER_CONTEXT_AUDIT_FILE
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
          "--sample-test-command", sampleTestCommand,
          "--sample-test-source", "command",
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
      assertEqual("checkpoint_unchanged_status", unchangedPayload.status, "GUARD_PROGRESS_REQUIRED", "status")
      assertEqual(
        "checkpoint_unchanged_source_guard",
        unchangedPayload.source_guard_passed,
        false,
        "source_guard_passed",
      )
      assertEqual(
        "checkpoint_unchanged_ready_for_full",
        unchangedPayload.ready_for_project_full,
        false,
        "ready_for_project_full",
      )
      assertEqual(
        "checkpoint_unchanged_project_full",
        unchangedPayload.project_full_executed,
        false,
        "project_full_executed",
      )
      assertEqual("checkpoint_unchanged_loop", unchangedPayload.loop?.decision, "continue", "loop.decision")
      assertCond(
        "checkpoint_unchanged_scalar_guidance",
        Array.isArray(unchangedPayload.metric_budget) || typeof unchangedPayload.next_action === "string",
        "cheap Guard response must retain bounded scalar guidance",
      )
      const afterFailureSystemOutput = { system: [] }
      await systemHook(
        { sessionID: "command-policy-self-check", model: {} },
        afterFailureSystemOutput,
      )
      assertEqual(
        "controller_system_context_stable_after_failure",
        afterFailureSystemOutput.system[0],
        frozenControllerContext,
        "controller context",
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
      const repairedPayload = parseJson("command_policy_pass_payload", repairedResult.output)
      assertEqual("command_policy_pass_project_full_top", repairedPayload.project_full_executed, true, "project_full_executed")
      assertEqual(
        "command_policy_pass_project_full_build_guard",
        repairedPayload.build_test_guard?.project_full_executed,
        true,
        "build_test_guard.project_full_executed",
      )
      assertEqual(
        "command_policy_pass_project_full_receipt",
        repairedPayload.formal_verification_receipt?.build_test?.project_full_executed,
        true,
        "formal_verification_receipt.build_test.project_full_executed",
      )
      const successPath = normalizeToolResult(repairedResult)
      assertEqual("command_policy_pass_decision", successPath.loop?.decision, "stop", "loop.decision")
      assertEqual("command_policy_pass_reason", successPath.loop?.termination_reason, "PASS", "termination_reason")
      const serializedCommandState = repairedResult.metadata?.command_loop_state
      assertCond(
        "command_policy_state_exported",
        Boolean(serializedCommandState && typeof serializedCommandState === "object"),
        "command loop state was not exported for runner handoff",
      )
      // The interactive fixture omitted Language from its command text. A
      // real batch controller resolves that field before first launch; model
      // the exact controller-owned transfer state used by the runner.
      const batchCommandState = JSON.parse(JSON.stringify(serializedCommandState))
      batchCommandState.policy.identity.language = "java"
      batchCommandState.target_identity_context = ""
      const baselineContextFile = path.join(tempRoot, "baseline-capture.json")
      await writeFile(
        baselineContextFile,
        JSON.stringify({ payload: parseJson("controller_baseline_context", sealProbe.stdout) }),
        "utf8",
      )

      Object.assign(process.env, {
        SMELL_PROJECT_ROOT: fixtureRoot,
        SMELL_LANGUAGE: "java",
        SMELL_SMELL: "long_method",
        SMELL_LOCATION: "src/main/java/SelfCheckSample.java:2",
        SMELL_VERIFICATION_MODE: "project_full",
        SMELL_SAMPLE_TEST_COMMAND: sampleTestCommand,
        SMELL_SAMPLE_TEST_SOURCE: "command",
        SMELL_BASELINE_CONTEXT_FILE: baselineContextFile,
      })
      delete process.env.SMELL_BASELINE_SEAL
      delete process.env.SMELL_COMMAND_LOOP_STATE_JSON
      const reloadedWithoutState = await pluginModule.SmellPlugin({ worktree: fixtureRoot })
      try {
        await reloadedWithoutState.tool.smell_verify.execute(verifyArgs, {
          sessionID: "batch-reload-no-state",
          agent: "java-refactor-agent",
          directory: fixtureRoot,
        })
        throw new SelfCheckError("batch_reload_no_state", "Batch identity without command state was accepted.", {})
      } catch (error) {
        assertCond(
          "batch_reload_without_state_fails_closed",
          String(error?.message || error).includes("COMMAND_POLICY_STATE_MISSING"),
          String(error?.message || error),
        )
      }
      process.env.SMELL_COMMAND_LOOP_STATE_JSON = JSON.stringify(batchCommandState)
      process.env.SMELL_LOCATION = "src/main/java/OtherSample.java:1"
      const reloadedWithWrongIdentity = await pluginModule.SmellPlugin({ worktree: fixtureRoot })
      try {
        await reloadedWithWrongIdentity.tool.smell_verify.execute(verifyArgs, {
          sessionID: "batch-reload-wrong-identity",
          agent: "java-refactor-agent",
          directory: fixtureRoot,
        })
        throw new SelfCheckError(
          "batch_reload_wrong_identity",
          "Stale command state was accepted for another target.",
          {},
        )
      } catch (error) {
        assertCond(
          "batch_reload_identity_mismatch_fails_closed",
          String(error?.message || error).includes("COMMAND_POLICY_STATE_IDENTITY_MISMATCH"),
          String(error?.message || error),
        )
      }
      process.env.SMELL_LOCATION = "src/main/java/SelfCheckSample.java:2"
      process.env.SMELL_SAMPLE_TEST_SOURCE = "dataset"
      const reloadedWithWrongCommandSource = await pluginModule.SmellPlugin({ worktree: fixtureRoot })
      try {
        await reloadedWithWrongCommandSource.tool.smell_verify.execute(verifyArgs, {
          sessionID: "batch-reload-wrong-command-source",
          agent: "java-refactor-agent",
          directory: fixtureRoot,
        })
        throw new SelfCheckError(
          "batch_reload_wrong_command_source",
          "Stale command state was accepted with another sample-test source.",
          {},
        )
      } catch (error) {
        assertCond(
          "batch_reload_command_source_mismatch_fails_closed",
          String(error?.message || error).includes("COMMAND_POLICY_STATE_IDENTITY_MISMATCH")
            && String(error?.message || error).includes("sample_test_source"),
          String(error?.message || error),
        )
      }
      process.env.SMELL_SAMPLE_TEST_SOURCE = "command"
      const reloadedWithoutSeal = await pluginModule.SmellPlugin({ worktree: fixtureRoot })
      try {
        await reloadedWithoutSeal.tool.smell_verify.execute(verifyArgs, {
          sessionID: "batch-reload-no-seal",
          agent: "java-refactor-agent",
          directory: fixtureRoot,
        })
        throw new SelfCheckError("batch_reload_no_seal", "Restored command state without its external seal was accepted.", {})
      } catch (error) {
        assertCond(
          "batch_reload_without_seal_fails_closed",
          String(error?.message || error).includes("CHECKPOINT_CONTROLLER_SEAL_MISSING"),
          String(error?.message || error),
        )
      }
      process.env.SMELL_BASELINE_SEAL = controllerSeal
      const reloadedWithSeal = await pluginModule.SmellPlugin({ worktree: fixtureRoot })
      const restoredSystemOutput = { system: [] }
      await reloadedWithSeal["experimental.chat.system.transform"](
        { sessionID: "batch-reload-with-seal", model: {} },
        restoredSystemOutput,
      )
      assertCond(
        "batch_reload_restores_stable_system_context",
        restoredSystemOutput.system[0].includes("Frozen target:")
          && !restoredSystemOutput.system[0].includes(batchCommandState.policy.loop.instruction),
        "batch restart did not hydrate stable target context without copying mutable loop instruction",
      )
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
        "SMELL_SAMPLE_TEST_COMMAND",
        "SMELL_SAMPLE_TEST_SOURCE",
        "SMELL_COMMAND_LOOP_STATE_JSON",
        "SMELL_BASELINE_SEAL",
        "SMELL_BASELINE_CONTEXT_FILE",
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

async function runGuardProgressGateSelfCheck() {
  const tempRoot = await mkdtemp(path.join(os.tmpdir(), "guard-progress-plugin-self-check-"))
  const fakeBridge = path.join(tempRoot, "guard_progress_bridge.py")
  const stateFile = path.join(tempRoot, "guard-progress-count.txt")
  const logFile = path.join(tempRoot, "guard-progress-commands.jsonl")
  const fakeSource = `
import json
import os
import sys
from pathlib import Path

command = sys.argv[1]
guard_progress_only = command == "verify" and "--guard-progress-only" in sys.argv
focused_preflight_only = command == "verify" and "--focused-preflight-only" in sys.argv
logged_command = (
    "guard-progress" if guard_progress_only
    else "focused-preflight" if focused_preflight_only
    else command
)
case = json.loads(os.environ.get("SMELL_PREFLIGHT_CASE", "{}"))
log_path = Path(os.environ["SMELL_PREFLIGHT_LOG"])
with log_path.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps({"case": case.get("name"), "command": logged_command, "argv": sys.argv[2:]}) + "\\n")

if command == "resolve-command":
    verification_mode = case.get("verification_mode", "project_full")
    payload = {
        "task": "Continue the current smell refactoring task.",
        "verification_mode": verification_mode,
        "refactoring_backend": "direct",
        "allow_test_changes": False,
        "checkpoint_required": bool(case.get("checkpoint_required")),
        "identity": {
            "project_root": case["project_root"],
            "project_override_root": "",
            "language": case["language"],
            "smell": case["smell"],
            "location": case["location"],
            "target_context_json": "",
            "verification_mode": verification_mode,
            "sample_test_location": "",
            "sample_test_command": case.get("sample_test_command", ""),
            "build_command": case.get("build_command", ""),
            "project_test_command": case.get("project_test_command", ""),
            "verification_cwd": case.get("verification_cwd", ""),
            "verification_command_source": case.get("verification_command_source", ""),
            "sample_test_source": case.get("sample_test_source", ""),
        },
        "loop": {
            "mode": "verify-failure",
            "max_smell_verify_cycles": int(case.get("max_smell_verify_cycles", 2)),
            "no_progress_limit": int(case.get("no_progress_limit", 1)),
            "allowed_failure_groups": ["smell", "compile", "test"],
            "instruction": "continue one narrow edit",
            "sample_deadline_seconds": 1800,
        },
    }
elif command == "capture-baseline":
    payload = {
        "success": True,
        "status": "BASELINE_CAPTURED",
        "baseline_seal": "controller-seal",
        "metrics": {},
        "resolution_plan": {"metric_budget": [case.get("budget", {})]},
    }
elif guard_progress_only:
    state_path = Path(os.environ["SMELL_PREFLIGHT_STATE"])
    count = int(state_path.read_text(encoding="utf-8") or "0") + 1
    state_path.write_text(str(count), encoding="utf-8")
    ready = count > int(case.get("early_calls", 0))
    budget = dict(case.get("budget", {}))
    if ready:
        budget["required_reduction"] = 0
    elif case.get("progress_each_call"):
        reduction = max(0, count - 1)
        if isinstance(budget.get("current"), (int, float)):
            budget["current"] = budget["current"] - reduction
        if isinstance(budget.get("required_reduction"), (int, float)):
            budget["required_reduction"] = max(1, budget["required_reduction"] - reduction)
    payload = {
        "schema_version": "smell.guard-progress/v1",
        "success": ready,
        "status": "GUARD_PROGRESS_PASSED" if ready else "GUARD_PROGRESS_REQUIRED",
        "applicable": True,
        "checkpoint_required": True,
        "source_guard_passed": ready,
        "ready_for_project_full": ready,
        "project_full_executed": False,
        "metric_budget": [budget],
        "next_action": "continue one narrow production edit" if not ready else "",
        "source_guard_feedback": {
            "next_action": "repair the exact source Guard contract" if not ready else "",
            "progress_observation": {
                "metric_deficit": budget.get("required_reduction", 0),
                "structural_failure_count": 0 if ready else int(case.get("structural_failure_count", 0)),
                "candidate_revision": (
                    f"revision-{count}"
                    if case.get("candidate_revision_each_call")
                    else case.get("candidate_revision", "")
                ),
            },
        },
    }
    if case.get("malformed_progress"):
        payload["project_full_executed"] = True
elif focused_preflight_only:
    focused_status = case.get("focused_status", "NOT_APPLICABLE")
    focused_generation = int(Path(os.environ["SMELL_PREFLIGHT_STATE"]).read_text(encoding="utf-8") or "0")
    focused_summary = (
        f"compile error stage {focused_generation}"
        if case.get("focused_progress_each_call")
        else "compile error in changed target"
    )
    payload = {
        "schema_version": 1,
        "type": "focused_preflight",
        "success": focused_status != "FAILED",
        "status": focused_status,
        "acceptance": False,
        "project_full_executed": False,
        "cache_scope": "compiler_outputs_only",
        "test_result_reused": False,
        "pass_reused": False,
        "message": "focused diagnostic",
        "execution": {
            "success": focused_status == "READY",
            "returncode": 17 if focused_status == "FAILED" else 0,
            "summary_text": focused_summary if focused_status == "FAILED" else "",
        } if focused_status != "NOT_APPLICABLE" else None,
    }
elif command == "verify":
    payload = {
        "success": True,
        "accepted": True,
        "progress": True,
        "project_full_executed": True,
        "status": "PASS",
        "resolution": "resolved",
        "continue_hint": "",
        "smell_guard": {"success": True, "failure_count": 0, "results": []},
        "build_test_guard": {"success": True},
        "snapshot": None,
        "artifacts": {},
        "formal_verification_receipt": {
            "schema_version": "smell.formal-verification-receipt/v1",
            "terminal_stage": "formal_verify",
            "status": "PASS",
            "success": True,
            "accepted": True,
            "resolution": "resolved",
            "candidate_identity": {
                "baseline_revision": "controller-seal",
                "baseline_tree": "",
                "production_diff": "candidate-diff",
                "test_tree": "test-tree",
                "verification_config_tree": "verification-config-tree",
            },
            "outcome": "pass",
            "diagnostic_signature": "PASS",
            "guard": {"success": True, "failure_count": 0},
            "build_test": {"success": True, "project_full_executed": True, "test_status": "passed"},
            "fresh_isolation": None,
            "artifact_refs": {},
        },
    }
else:
    payload = {"success": False, "status": "UNEXPECTED_COMMAND", "command": command}

print(json.dumps(payload))
if guard_progress_only and case.get("guard_progress_exit_code"):
    raise SystemExit(int(case["guard_progress_exit_code"]))
`
  await writeFile(fakeBridge, fakeSource, "utf8")
  await writeFile(stateFile, "0", "utf8")
  await writeFile(logFile, "", "utf8")
  const envBefore = { ...process.env }
  process.env.SMELL_BRIDGE_FILE = fakeBridge
  process.env.SMELL_PREFLIGHT_STATE = stateFile
  process.env.SMELL_PREFLIGHT_LOG = logFile
  process.env.SMELL_SESSION_STATE_ROOT = path.join(tempRoot, "session-state")
  delete process.env.SMELL_BATCH_RUN
  try {
    const compiledFile = await compilePluginForSelfCheck(tempRoot)
    const pluginModule = await import(
      `${pathToFileURL(compiledFile).href}?guard_progress=${Date.now()}`
    )
    const cheapGateHook = pluginModule.SmellPlugin?.__selfTest?.usesCheapGuardProgressGate
    const checkpointState = { policy: { checkpoint_required: true } }
    assertCond(
      "guard_progress_java_language_dispatch",
      typeof cheapGateHook === "function"
        && cheapGateHook({ language: "java", location: "Foo.java:1" }, checkpointState),
      "Java language did not enter the source-only Guard progress gate",
    )
    assertCond(
      "guard_progress_java_location_dispatch",
      cheapGateHook({ language: "", location: "Foo.java:1" }, checkpointState),
      "Java location did not enter the source-only Guard progress gate",
    )
    process.env.SMELL_BUILD_JOBS = "1"
    const candidateShellGateResults = []
    for (const language of ["python", "c", "cpp"]) {
      const projectRoot = path.join(tempRoot, `candidate-shell-gate-${language}`)
      await mkdir(projectRoot, { recursive: true })
      process.env.SMELL_PREFLIGHT_CASE = JSON.stringify({
        name: `candidate-shell-gate-${language}`,
        project_root: projectRoot,
        language,
        smell: "long_method",
        location: `sample.${language === "python" ? "py" : "cc"}:method=target|line=1`,
        checkpoint_required: true,
        budget: {},
      })
      const plugin = await pluginModule.SmellPlugin({ worktree: projectRoot })
      const sessionID = `candidate-shell-gate-${language}`
      await plugin["command.execute.before"](
        {
          command: "smell-refactor-run",
          sessionID,
          arguments: `--verification-mode=project_full -- Project root: ${projectRoot}; Language: ${language}; Smell type: long_method; Target location: sample.cc:method=target|line=1`,
        },
        { parts: [] },
      )
      for (const command of [
        "git status --short",
        "python -m pytest -q",
        "touch arbitrary-output",
      ]) {
        let message = ""
        try {
          await plugin["tool.execute.before"](
            { tool: "bash", sessionID },
            { args: { command } },
          )
        } catch (error) {
          message = String(error?.message || error)
        }
        assertCond(
          `candidate_shell_${language}_blocks_${command}`,
          message.includes("SMELL_CANDIDATE_SHELL_FORBIDDEN"),
          `controller-owned project_full bash was not rejected: ${command}`,
        )
      }
      const childSessionID = `${sessionID}-child`
      await plugin.event({
        event: {
          type: "session.created",
          properties: {
            sessionID: childSessionID,
            info: { parentID: sessionID },
          },
        },
      })
      let childMessage = ""
      try {
        await plugin["tool.execute.before"](
          { tool: "bash", sessionID: childSessionID },
          { args: { command: "cmake --build build" } },
        )
      } catch (error) {
        childMessage = String(error?.message || error)
      }
      assertCond(
        `candidate_shell_${language}_blocks_child_session`,
        childMessage.includes("SMELL_CANDIDATE_SHELL_FORBIDDEN"),
        "fresh child session escaped the controller-owned candidate shell policy",
      )
      const grandchildSessionID = `${childSessionID}-child`
      await plugin.event({
        event: {
          type: "session.created",
          properties: {
            info: { id: grandchildSessionID, parentID: childSessionID },
          },
        },
      })
      let grandchildMessage = ""
      try {
        await plugin["tool.execute.before"](
          { tool: "bash", sessionID: grandchildSessionID },
          { args: { command: "python -m pytest" } },
        )
      } catch (error) {
        grandchildMessage = String(error?.message || error)
      }
      assertCond(
        `candidate_shell_${language}_blocks_nested_child_session`,
        grandchildMessage.includes("SMELL_CANDIDATE_SHELL_FORBIDDEN"),
        "nested child session escaped the controller-owned candidate shell policy",
      )
      const intermediateSessionID = `${sessionID}-unprotected-intermediate`
      await plugin.event({
        event: {
          type: "session.created",
          properties: {
            sessionID: intermediateSessionID,
            info: { parentID: sessionID },
          },
        },
      })
      process.env.SMELL_PREFLIGHT_CASE = JSON.stringify({
        name: `candidate-shell-unprotected-intermediate-${language}`,
        project_root: projectRoot,
        language,
        smell: "long_method",
        location: `sample.${language === "python" ? "py" : "cc"}:method=target|line=1`,
        checkpoint_required: true,
        verification_mode: "sample_optimized",
        budget: {},
      })
      await plugin["command.execute.before"](
        {
          command: "smell-refactor-run",
          sessionID: intermediateSessionID,
          arguments: `--verification-mode=sample_optimized -- Project root: ${projectRoot}; Language: ${language}; Smell type: long_method; Target location: sample.cc:method=target|line=1`,
        },
        { parts: [] },
      )
      let intermediateMessage = ""
      try {
        await plugin["tool.execute.before"](
          { tool: "bash", sessionID: intermediateSessionID },
          { args: { command: "cmake --build build" } },
        )
      } catch (error) {
        intermediateMessage = String(error?.message || error)
      }
      assertCond(
        `candidate_shell_${language}_blocks_child_with_unprotected_state`,
        intermediateMessage.includes("SMELL_CANDIDATE_SHELL_FORBIDDEN"),
        "a child-owned unprotected command state masked its protected parent",
      )
      const nestedThroughUnprotectedID = `${intermediateSessionID}-child`
      await plugin.event({
        event: {
          type: "session.created",
          properties: {
            info: { id: nestedThroughUnprotectedID, parentID: intermediateSessionID },
          },
        },
      })
      let nestedThroughUnprotectedMessage = ""
      try {
        await plugin["tool.execute.before"](
          { tool: "bash", sessionID: nestedThroughUnprotectedID },
          { args: { command: "cmake --build build" } },
        )
      } catch (error) {
        nestedThroughUnprotectedMessage = String(error?.message || error)
      }
      assertCond(
        `candidate_shell_${language}_blocks_nested_child_through_unprotected_state`,
        nestedThroughUnprotectedMessage.includes("SMELL_CANDIDATE_SHELL_FORBIDDEN"),
        "an unprotected intermediate command state masked its protected ancestor",
      )
      await plugin.event({
        event: {
          type: "session.deleted",
          properties: { info: { id: sessionID } },
        },
      })
      let orphanedChildMessage = ""
      try {
        await plugin["tool.execute.before"](
          { tool: "bash", sessionID: childSessionID },
          { args: { command: "ninja --jobs=8" } },
        )
      } catch (error) {
        orphanedChildMessage = String(error?.message || error)
      }
      assertCond(
        `candidate_shell_${language}_survives_parent_deletion`,
        orphanedChildMessage.includes("SMELL_CANDIDATE_SHELL_FORBIDDEN"),
        "a live child lost inherited shell protection when its parent was deleted",
      )
      await plugin.dispose?.()
      candidateShellGateResults.push({ language, blocked: 8 })
    }
    const buildGateResults = []
    const buildGateCases = [
      {
        name: "python",
        language: "python",
        blocked: [
          "ninja -j8",
          "ninja --jobs=8",
          "ninja --jobs 8",
          "ninja --jobs",
          "command ninja --jobs=8",
          "env NINJA_STATUS=brief ninja --jobs 8",
          "/usr/bin/env NINJA_STATUS=brief ninja --jobs=8",
          "bash -lc 'ninja -j8'",
          "sh -lc 'ninja --jobs=8'",
          'bash -lc "ninja --jobs 8"',
        ],
        allowed: [
          "ninja",
          "ninja -j1",
          "ninja --jobs=1",
          "ninja --jobs 1",
          "command ninja --jobs=1",
          "env NINJA_STATUS=brief ninja --jobs 1",
          "bash -lc 'ninja -j1'",
          'ninja -j${SMELL_BUILD_JOBS:-1}',
          'ninja --jobs ${SMELL_BUILD_JOBS:-1}',
          'ninja --jobs="${SMELL_BUILD_JOBS:-1}"',
          "printf 'safe\\nninja --jobs=8\\n'",
          "echo 'ninja --jobs=8'",
          "# ninja --jobs=8",
          "cat <<'EOF'\nninja --jobs=8\nEOF",
          "bash -lc 'printf \"safe\\\\nninja -j8\\\\n\"'",
        ],
      },
      {
        name: "c",
        language: "c",
        blocked: [
          "make -j4",
          "/usr/bin/gmake -j 2",
          "cd src && make -j",
          "make --jobs=4",
          "make --jobs 4",
          "make --jobs",
          "command make --jobs=4",
          "env LC_ALL=C make --jobs 4",
          "/usr/bin/env LC_ALL=C make --jobs=4",
          "bash -lc 'make -j4'",
          "sh -lc 'make --jobs 4'",
          "MAKEFLAGS=-j8 make",
          "MAKEFLAGS='-j 8' make",
          "env MAKEFLAGS=--jobs=8 make",
          "bash -lc 'MAKEFLAGS=-j8 make'",
          "env SMELL_BUILD_JOBS=8 bash -lc 'make -j${SMELL_BUILD_JOBS:-1}'",
        ],
        allowed: [
          "make",
          "make -j1",
          "make --jobs=1",
          "make --jobs 1",
          "command make --jobs=1",
          "env LC_ALL=C make --jobs 1",
          "/usr/bin/env LC_ALL=C make --jobs=1",
          "bash -lc 'make -j1'",
          'make -j${SMELL_BUILD_JOBS:-1}',
          'make -j"${SMELL_BUILD_JOBS:-1}"',
          'make --jobs="${SMELL_BUILD_JOBS:-1}"',
          "MAKEFLAGS=-j1 make",
          "MAKEFLAGS='-j 1' make",
          "env SMELL_BUILD_JOBS=${SMELL_BUILD_JOBS:-1} bash -lc 'make -j${SMELL_BUILD_JOBS:-1}'",
          "printf 'make -j8\\n'",
          "cat <<EOF\nmake -j8\nEOF\necho done",
        ],
      },
      {
        name: "cpp",
        language: "cpp",
        blocked: [
          "cmake --build build-refactoragent -j4",
          "cmake --build out --parallel 3",
          "cmake --build out --parallel=2",
          "CMAKE_BUILD_PARALLEL_LEVEL=8 cmake --build out",
          "CMAKE_BUILD_PARALLEL_LEVEL=0 cmake --build out",
          "CMAKE_BUILD_PARALLEL_LEVEL= cmake --build out",
          "env CMAKE_BUILD_PARALLEL_LEVEL=8 cmake --build out",
          "/usr/bin/env CMAKE_BUILD_PARALLEL_LEVEL=8 cmake --build out",
          "command cmake --build out -j4",
          "bash -lc 'cmake --build out -j4'",
          "sh -lc 'cmake --build out --parallel 4'",
          'bash -lc "cmake --build out --parallel 4"',
          "env CMAKE_BUILD_PARALLEL_LEVEL=8 bash -lc 'cmake --build out'",
        ],
        allowed: [
          "cmake --build out",
          "cmake --build out -j1",
          "cmake --build out --parallel 1",
          "CMAKE_BUILD_PARALLEL_LEVEL=1 cmake --build out",
          "env CMAKE_BUILD_PARALLEL_LEVEL=1 cmake --build out",
          "/usr/bin/env CMAKE_BUILD_PARALLEL_LEVEL=1 cmake --build out",
          "command cmake --build out -j1",
          "bash -lc 'cmake --build out -j1'",
          'cmake --build out -j${SMELL_BUILD_JOBS:-1}',
          'cmake --build out --parallel ${SMELL_BUILD_JOBS:-1}',
          'cmake --build out --parallel "${SMELL_BUILD_JOBS:-1}"',
          'CMAKE_BUILD_PARALLEL_LEVEL=${SMELL_BUILD_JOBS:-1} cmake --build out',
          'env CMAKE_BUILD_PARALLEL_LEVEL="${SMELL_BUILD_JOBS:-1}" cmake --build out',
          "env CMAKE_BUILD_PARALLEL_LEVEL=1 bash -lc 'cmake --build out'",
          "printf '%s\\n' 'CMAKE_BUILD_PARALLEL_LEVEL=8 cmake --build out'",
        ],
      },
    ]
    for (const buildCase of buildGateCases) {
      const projectRoot = path.join(tempRoot, `build-gate-${buildCase.name}`)
      await mkdir(projectRoot, { recursive: true })
      process.env.SMELL_PREFLIGHT_CASE = JSON.stringify({
        name: `build-gate-${buildCase.name}`,
        project_root: projectRoot,
        language: buildCase.language,
        smell: "long_method",
        location: `sample.${buildCase.language === "python" ? "py" : "cc"}:method=target|line=1`,
        checkpoint_required: true,
        budget: {},
      })
      const plugin = await pluginModule.SmellPlugin({ worktree: projectRoot })
      const sessionID = `build-gate-${buildCase.name}`
      await plugin["command.execute.before"](
        {
          command: "smell-refactor-run",
          sessionID,
          arguments: `--verification-mode=project_full -- Project root: ${projectRoot}; Language: ${buildCase.language}; Smell type: long_method; Target location: sample.cc:method=target|line=1`,
        },
        { parts: [] },
      )
      for (const command of buildCase.blocked) {
        let message = ""
        try {
          await plugin["tool.execute.before"](
            { tool: "bash", sessionID },
            { args: { command } },
          )
        } catch (error) {
          message = String(error?.message || error)
        }
        assertCond(
          `candidate_shell_${buildCase.name}_blocks_${command}`,
          message.includes("SMELL_CANDIDATE_SHELL_FORBIDDEN"),
          `command was not rejected: ${command}`,
        )
      }
      for (const command of buildCase.allowed) {
        let message = ""
        try {
          await plugin["tool.execute.before"](
            { tool: "bash", sessionID },
            { args: { command } },
          )
        } catch (error) {
          message = String(error?.message || error)
        }
        assertCond(
          `candidate_shell_${buildCase.name}_blocks_formerly_allowed_${command}`,
          message.includes("SMELL_CANDIDATE_SHELL_FORBIDDEN"),
          `controller-owned project_full bash was not rejected: ${command}`,
        )
      }
      await plugin.dispose?.()
      buildGateResults.push({
        language: buildCase.language,
        blocked: buildCase.blocked.length + buildCase.allowed.length,
      })
    }

    process.env.SMELL_BUILD_JOBS = "2"
    const capTwoRoot = path.join(tempRoot, "build-gate-cap-two")
    await mkdir(capTwoRoot, { recursive: true })
    process.env.SMELL_PREFLIGHT_CASE = JSON.stringify({
      name: "build-gate-cap-two",
      project_root: capTwoRoot,
      language: "cpp",
      smell: "long_method",
      location: "sample.cc:method=target|line=1",
      checkpoint_required: true,
      budget: {},
    })
    const capTwoPlugin = await pluginModule.SmellPlugin({ worktree: capTwoRoot })
    await capTwoPlugin["command.execute.before"](
      {
        command: "smell-refactor-run",
        sessionID: "build-gate-cap-two",
        arguments: `--verification-mode=project_full -- Project root: ${capTwoRoot}; Language: cpp; Smell type: long_method; Target location: sample.cc:method=target|line=1`,
      },
      { parts: [] },
    )
    for (const command of [
      "cmake --build out --parallel 2",
      "make --jobs=2",
      "ninja --jobs 2",
      "CMAKE_BUILD_PARALLEL_LEVEL=2 command cmake --build out",
      "bash -lc 'cmake --build out --parallel 2'",
      "MAKEFLAGS=-j2 make",
      "env SMELL_BUILD_JOBS=2 bash -lc 'make -j${SMELL_BUILD_JOBS:-1}'",
    ]) {
      let message = ""
      try {
        await capTwoPlugin["tool.execute.before"](
          { tool: "bash", sessionID: "build-gate-cap-two" },
          { args: { command } },
        )
      } catch (error) {
        message = String(error?.message || error)
      }
      assertCond(
        `candidate_shell_cap_two_blocks_${command}`,
        message.includes("SMELL_CANDIDATE_SHELL_FORBIDDEN"),
        `controller-owned project_full bash was not rejected: ${command}`,
      )
    }
    for (const command of [
      "cmake --build out --parallel 3",
      "make --jobs=3",
      "ninja --jobs 3",
      "CMAKE_BUILD_PARALLEL_LEVEL=3 command cmake --build out",
      "bash -lc 'cmake --build out --parallel 3'",
      "MAKEFLAGS=-j3 make",
      "env SMELL_BUILD_JOBS=3 bash -lc 'make -j${SMELL_BUILD_JOBS:-1}'",
    ]) {
      let capTwoMessage = ""
      try {
        await capTwoPlugin["tool.execute.before"](
          { tool: "bash", sessionID: "build-gate-cap-two" },
          { args: { command } },
        )
      } catch (error) {
        capTwoMessage = String(error?.message || error)
      }
      assertCond(
        `candidate_shell_cap_two_blocks_high_parallel_${command}`,
        capTwoMessage.includes("SMELL_CANDIDATE_SHELL_FORBIDDEN"),
        `controller-owned project_full bash was not rejected: ${command}`,
      )
    }
    await capTwoPlugin.dispose?.()

    process.env.SMELL_BUILD_JOBS = "1"
    const nonProjectFullRoot = path.join(tempRoot, "candidate-shell-non-project-full")
    await mkdir(nonProjectFullRoot, { recursive: true })
    process.env.SMELL_PREFLIGHT_CASE = JSON.stringify({
      name: "candidate-shell-non-project-full",
      project_root: nonProjectFullRoot,
      language: "cpp",
      smell: "long_method",
      location: "sample.cc:method=target|line=1",
      checkpoint_required: true,
      verification_mode: "sample_optimized",
      budget: {},
    })
    const nonProjectFullPlugin = await pluginModule.SmellPlugin({ worktree: nonProjectFullRoot })
    await nonProjectFullPlugin["command.execute.before"](
      {
        command: "smell-refactor-run",
        sessionID: "candidate-shell-non-project-full",
        arguments: `--verification-mode=sample_optimized -- Project root: ${nonProjectFullRoot}; Language: cpp; Smell type: long_method; Target location: sample.cc:method=target|line=1`,
      },
      { parts: [] },
    )
    await nonProjectFullPlugin["tool.execute.before"](
      { tool: "bash", sessionID: "candidate-shell-non-project-full" },
      { args: { command: "cmake --build out --parallel 1" } },
    )
    let nonProjectFullMessage = ""
    try {
      await nonProjectFullPlugin["tool.execute.before"](
        { tool: "bash", sessionID: "candidate-shell-non-project-full" },
        { args: { command: "cmake --build out --parallel 2" } },
      )
    } catch (error) {
      nonProjectFullMessage = String(error?.message || error)
    }
    assertCond(
      "candidate_shell_non_project_full_keeps_existing_build_cap",
      nonProjectFullMessage.includes("SMELL_BUILD_PARALLELISM_EXCEEDED"),
      "non-project_full checkpoint session did not preserve its existing bash policy",
    )
    await nonProjectFullPlugin.dispose?.()

    process.env.SMELL_BUILD_JOBS = "1"
    const resumeRoot = path.join(tempRoot, "build-gate-resume")
    await mkdir(resumeRoot, { recursive: true })
    const resumeLocation = "resume.cc:method=target|line=1"
    process.env.SMELL_PREFLIGHT_CASE = JSON.stringify({
      name: "build-gate-resume",
      project_root: resumeRoot,
      language: "cpp",
      smell: "long_method",
      location: resumeLocation,
      checkpoint_required: true,
      early_calls: 1,
      budget: {},
    })
    await writeFile(stateFile, "0", "utf8")
    const initialResumePlugin = await pluginModule.SmellPlugin({ worktree: resumeRoot })
    await initialResumePlugin["command.execute.before"](
      {
        command: "smell-refactor-run",
        sessionID: "build-gate-resume-initial",
        arguments: `--verification-mode=project_full -- Project root: ${resumeRoot}; Language: cpp; Smell type: long_method; Target location: ${resumeLocation}`,
      },
      { parts: [] },
    )
    const initialResumeResult = await initialResumePlugin.tool.smell_verify.execute(
      {
        projectRoot: resumeRoot,
        smell: "long_method",
        location: resumeLocation,
        verificationMode: "project_full",
      },
      {
        sessionID: "build-gate-resume-initial",
        agent: "smell-refactor-agent",
        directory: resumeRoot,
      },
    )
    const resumeState = initialResumeResult.metadata?.command_loop_state
    assertCond(
      "candidate_shell_resume_state_exported",
      Boolean(resumeState && typeof resumeState === "object"),
      "initial checkpoint session did not export command state",
    )
    await initialResumePlugin.dispose?.()
    Object.assign(process.env, {
      SMELL_PROJECT_ROOT: resumeRoot,
      SMELL_LANGUAGE: "cpp",
      SMELL_SMELL: "long_method",
      SMELL_LOCATION: resumeLocation,
      SMELL_VERIFICATION_MODE: "project_full",
      SMELL_COMMAND_LOOP_STATE_JSON: JSON.stringify(resumeState),
    })
    const resumedPlugin = await pluginModule.SmellPlugin({ worktree: resumeRoot })
    let resumedMessage = ""
    try {
      await resumedPlugin["tool.execute.before"](
        { tool: "bash", sessionID: "build-gate-resumed" },
        { args: { command: "cmake --build out -j4" } },
      )
    } catch (error) {
      resumedMessage = String(error?.message || error)
    }
    assertCond(
      "candidate_shell_resumed_state_blocks_bash",
      resumedMessage.includes("SMELL_CANDIDATE_SHELL_FORBIDDEN"),
      "restored batch command state did not enforce candidate shell isolation",
    )
    await resumedPlugin.dispose?.()

    process.env.SMELL_LOCATION = "other.cc:method=other|line=1"
    const mismatchedResumePlugin = await pluginModule.SmellPlugin({ worktree: resumeRoot })
    let mismatchMessage = ""
    try {
      await mismatchedResumePlugin["tool.execute.before"](
        { tool: "bash", sessionID: "build-gate-resume-mismatch" },
        { args: { command: "cmake --build out -j4" } },
      )
    } catch (error) {
      mismatchMessage = String(error?.message || error)
    }
    assertCond(
      "candidate_shell_resume_identity_mismatch_fails_closed",
      mismatchMessage.includes("COMMAND_POLICY_STATE_IDENTITY_MISMATCH"),
      "batch identity mismatch did not fail closed",
    )
    await mismatchedResumePlugin.dispose?.()

    process.env.SMELL_LOCATION = resumeLocation
    process.env.SMELL_COMMAND_LOOP_STATE_JSON = '{"schema_version":4}'
    const malformedResumePlugin = await pluginModule.SmellPlugin({ worktree: resumeRoot })
    let malformedResumeMessage = ""
    try {
      await malformedResumePlugin["tool.execute.before"](
        { tool: "bash", sessionID: "build-gate-resume-malformed" },
        { args: { command: "cmake --build out -j4" } },
      )
    } catch (error) {
      malformedResumeMessage = String(error?.message || error)
    }
    assertCond(
      "candidate_shell_resume_malformed_state_fails_closed",
      malformedResumeMessage.includes("COMMAND_POLICY_STATE_INVALID"),
      "malformed resumed command state did not fail closed",
    )
    await malformedResumePlugin.dispose?.()
    for (const key of [
      "SMELL_PROJECT_ROOT",
      "SMELL_LANGUAGE",
      "SMELL_SMELL",
      "SMELL_LOCATION",
      "SMELL_VERIFICATION_MODE",
      "SMELL_COMMAND_LOOP_STATE_JSON",
    ]) delete process.env[key]

    process.env.SMELL_BUILD_JOBS = "1"
    const bypassCases = [
      { name: "java", language: "java", checkpoint_required: true },
      { name: "noncheckpoint", language: "python", checkpoint_required: false },
    ]
    for (const bypass of bypassCases) {
      const projectRoot = path.join(tempRoot, `build-gate-${bypass.name}`)
      await mkdir(projectRoot, { recursive: true })
      process.env.SMELL_PREFLIGHT_CASE = JSON.stringify({
        ...bypass,
        project_root: projectRoot,
        smell: "long_method",
        location: bypass.language === "java" ? "Sample.java:1" : "sample.py:1",
        budget: {},
      })
      const plugin = await pluginModule.SmellPlugin({ worktree: projectRoot })
      const sessionID = `build-gate-${bypass.name}`
      await plugin["command.execute.before"](
        {
          command: "smell-refactor-run",
          sessionID,
          arguments: `--verification-mode=project_full -- Project root: ${projectRoot}; Language: ${bypass.language}; Smell type: long_method; Target location: ${bypass.language === "java" ? "Sample.java:1" : "sample.py:1"}`,
        },
        { parts: [] },
      )
      for (const command of [
        "cmake --build out -j8",
        "make --jobs=8",
        "bash -lc 'ninja --jobs=8'",
        "env CMAKE_BUILD_PARALLEL_LEVEL=8 cmake --build out",
        "MAKEFLAGS=-j8 make",
        "env SMELL_BUILD_JOBS=8 bash -lc 'make -j${SMELL_BUILD_JOBS:-1}'",
      ]) {
        await plugin["tool.execute.before"](
          { tool: "bash", sessionID },
          { args: { command } },
        )
      }
      if (bypass.language === "java") {
        for (const command of ["mvn test", "./gradlew test"]) {
          let message = ""
          try {
            await plugin["tool.execute.before"](
              { tool: "bash", sessionID },
              { args: { command } },
            )
          } catch (error) {
            message = String(error?.message || error)
          }
          assertCond(
            `candidate_shell_java_keeps_${command}`,
            message.includes("Do not run Maven or Gradle directly"),
            `existing Java build rule did not reject: ${command}`,
          )
        }
      }
      await plugin.dispose?.()
    }
    const unownedPlugin = await pluginModule.SmellPlugin({ worktree: tempRoot })
    await unownedPlugin.event({
      event: {
        type: "session.created",
        properties: {
          sessionID: "unowned-build-gate-child",
          info: { id: "unowned-build-gate-child", parentID: "unowned-build-gate" },
        },
      },
    })
    for (const command of [
      "make -j8",
      "make --jobs=8",
      "bash -lc 'ninja --jobs=8'",
      "env CMAKE_BUILD_PARALLEL_LEVEL=8 cmake --build out",
      "MAKEFLAGS=-j8 make",
      "env SMELL_BUILD_JOBS=8 bash -lc 'make -j${SMELL_BUILD_JOBS:-1}'",
    ]) {
      await unownedPlugin["tool.execute.before"](
        { tool: "bash", sessionID: "unowned-build-gate" },
        { args: { command } },
      )
      await unownedPlugin["tool.execute.before"](
        { tool: "bash", sessionID: "unowned-build-gate-child" },
        { args: { command } },
      )
    }
    await unownedPlugin.dispose?.()

    const replayCases = [
      {
        name: "55",
        language: "python",
        smell: "long_method",
        location: "sample55.py:method=target|line=1",
        budget: { metric: "meaningful_line_count", current: 91, passing_max: 80, required_reduction: 11, unit: "meaningful_line_count" },
      },
      {
        name: "57",
        language: "python",
        smell: "long_method",
        location: "sample57.py:method=target|line=1",
        budget: { metric: "meaningful_line_count", current: 84, passing_max: 80, required_reduction: 4, unit: "meaningful_line_count" },
      },
      {
        name: "185",
        language: "c",
        smell: "nested_complexity",
        location: "sample185.c:method=target|line=1",
        focused_status: "FAILED",
        budget: { metric: "max_nesting_depth", current: 6, passing_max: 4, required_reduction: 2, unit: "max_nesting_depth" },
      },
      {
        name: "java-long-method",
        language: "java",
        smell: "long_method",
        location: "Sample.java:1",
        budget: { metric: "ast_ncss", current: 91, passing_max: 80, required_reduction: 11, unit: "ast_ncss" },
      },
    ]
    const results = []
    for (const replay of replayCases) {
      const replayRoot = path.join(tempRoot, `replay-${replay.name}`)
      const controllerVerification = replay.language === "java"
        ? {
            build_command: "./mvnw -q -DskipTests package",
            project_test_command: "./mvnw -q test",
            verification_cwd: path.join(replayRoot, "module-a"),
            verification_command_source: "dataset",
            sample_test_command: "./mvnw -q -Dtest=FocusedTest test",
            sample_test_source: "dataset",
          }
        : {}
      await mkdir(replayRoot, { recursive: true })
      await writeFile(stateFile, "0", "utf8")
      await writeFile(logFile, "", "utf8")
      process.env.SMELL_PREFLIGHT_CASE = JSON.stringify({
        ...replay,
        project_root: replayRoot,
        checkpoint_required: true,
        early_calls: 2,
        progress_each_call: true,
        ...controllerVerification,
      })
      const { client: replayClient, calls: replayPromptCalls } = makeFakeClient()
      const plugin = await pluginModule.SmellPlugin({ worktree: replayRoot, client: replayClient })
      const sessionID = `guard-progress-${replay.name}`
      await plugin["command.execute.before"](
        {
          command: "smell-refactor-run",
          sessionID,
          arguments: `--verification-mode=project_full --max-smell-verify-cycles=2 -- Project root: ${replayRoot}; Language: ${replay.language}; Smell type: ${replay.smell}; Target location: ${replay.location}`,
        },
        { parts: [] },
      )
      const toolArgs = {
        projectRoot: replayRoot,
        smell: replay.smell,
        location: replay.location,
        verificationMode: "project_full",
        buildCommand: "false",
        projectTestCommand: "false",
        verificationCwd: "/tmp/model-controlled-cwd",
        verificationCommandSource: "cli",
        sampleTestCommand: "false",
        sampleTestSource: "command",
      }
      const toolContext = { sessionID, agent: "smell-refactor-agent", directory: replayRoot }
      const earlyStates = []
      for (let call = 0; call < 2; call += 1) {
        const early = await plugin.tool.smell_verify.execute(toolArgs, toolContext)
        const earlyPayload = parseJson(`guard_progress_${replay.name}_${call}`, early.output)
        assertEqual(`guard_progress_${replay.name}_${call}_status`, earlyPayload.status, "GUARD_PROGRESS_REQUIRED", "status")
        assertEqual(`guard_progress_${replay.name}_${call}_full`, earlyPayload.project_full_executed, false, "project_full_executed")
        assertEqual(`guard_progress_${replay.name}_${call}_focused_absent`, earlyPayload.focused_preflight, undefined, "focused_preflight")
        assertEqual(`guard_progress_${replay.name}_${call}_source_feedback`, earlyPayload.loop?.instruction, "repair the exact source Guard contract", "loop.instruction")
        assertEqual(`guard_progress_${replay.name}_${call}_loop`, earlyPayload.loop?.decision, "continue", "loop.decision")
        assertEqual(
          `guard_progress_${replay.name}_${call}_continuation`,
          early.metadata?.command_loop_state?.smell_verify_cycle_count,
          call + 1,
          "smell_verify_cycle_count",
        )
        assertEqual(
          `guard_progress_${replay.name}_${call}_no_progress`,
          early.metadata?.command_loop_state?.no_progress_count,
          0,
          "no_progress_count",
        )
        assertEqual(
          `guard_progress_${replay.name}_${call}_fingerprint`,
          Boolean(early.metadata?.command_loop_state?.last_failure_fingerprint),
          true,
          "last_failure_fingerprint present",
        )
        assertEqual(
          `guard_progress_${replay.name}_${call}_auto_enabled`,
          early.metadata?.auto_continuation?.enabled,
          true,
          "auto_continuation.enabled",
        )
        await plugin.event({
          event: { type: "session.idle", properties: { sessionID } },
        })
        await flush()
        assertEqual(
          `guard_progress_${replay.name}_${call}_manual_prompt`,
          replayPromptCalls.length,
          call + 1,
          "prompt count",
        )
        earlyStates.push(JSON.stringify(early.metadata?.command_loop_state))
      }
      assertEqual(`guard_progress_${replay.name}_state_tracks_progress`, earlyStates[1] === earlyStates[0], false, "state differs")
      const beforeCross = (await readFile(logFile, "utf8"))
        .trim().split(/\r?\n/).filter(Boolean).map((line) => JSON.parse(line))
      assertEqual(
        `guard_progress_${replay.name}_premature_full_count`,
        beforeCross.filter((item) => item.command === "verify").length,
        0,
        "verify count",
      )
      const crossed = await plugin.tool.smell_verify.execute(toolArgs, toolContext)
      const crossedPayload = parseJson(`guard_progress_${replay.name}_crossed`, crossed.output)
      assertEqual(`guard_progress_${replay.name}_crossed_status`, crossedPayload.status, "PASS", "status")
      assertEqual(`guard_progress_${replay.name}_crossed_loop`, crossedPayload.loop?.decision, "stop", "loop.decision")
      assertEqual(
        `guard_progress_${replay.name}_crossed_continuation`,
        crossed.metadata?.command_loop_state?.smell_verify_cycle_count,
        2,
        "smell_verify_cycle_count",
      )
      const commands = (await readFile(logFile, "utf8"))
        .trim().split(/\r?\n/).filter(Boolean).map((line) => JSON.parse(line))
      assertEqual(`guard_progress_${replay.name}_preflight_count`, commands.filter((item) => item.command === "guard-progress").length, 3, "guard-progress count")
      assertEqual(`guard_progress_${replay.name}_focused_count`, commands.filter((item) => item.command === "focused-preflight").length, 0, "focused-preflight count")
      assertEqual(`guard_progress_${replay.name}_full_count`, commands.filter((item) => item.command === "verify").length, 1, "verify count")
      if (replay.language === "java") {
        for (const command of commands.filter((item) => item.command !== "resolve-command")) {
          const argv = command.argv || []
          if (command.command !== "capture-baseline") {
            assertEqual(`guard_progress_${replay.name}_seal`, argv[argv.indexOf("--baseline-seal") + 1], "controller-seal", "baseline seal")
          }
          assertEqual(`guard_progress_${replay.name}_language`, argv[argv.indexOf("--language") + 1], "java", "language")
          assertEqual(`guard_progress_${replay.name}_smell`, argv[argv.indexOf("--smell") + 1], replay.smell, "smell")
          assertEqual(`guard_progress_${replay.name}_location`, argv[argv.indexOf("--location") + 1], replay.location, "location")
          assertEqual(
            `guard_progress_${replay.name}_build_command`,
            argv[argv.indexOf("--build-command") + 1],
            controllerVerification.build_command,
            "build command",
          )
          assertEqual(
            `guard_progress_${replay.name}_project_test_command`,
            argv[argv.indexOf("--project-test-command") + 1],
            controllerVerification.project_test_command,
            "project test command",
          )
          assertEqual(
            `guard_progress_${replay.name}_verification_cwd`,
            argv[argv.indexOf("--verification-cwd") + 1],
            controllerVerification.verification_cwd,
            "verification cwd",
          )
          assertEqual(
            `guard_progress_${replay.name}_verification_source`,
            argv[argv.indexOf("--verification-command-source") + 1],
            "dataset",
            "verification source",
          )
          assertEqual(
            `guard_progress_${replay.name}_sample_test_source`,
            argv[argv.indexOf("--sample-test-source") + 1],
            "dataset",
            "sample test source",
          )
          assertCond(
            `guard_progress_${replay.name}_model_commands_rejected`,
            !argv.includes("false") && !argv.includes("/tmp/model-controlled-cwd"),
            "model-controlled command fields reached the bridge",
          )
        }
      }
      results.push({ replay: replay.name, prematureFull: 0, finalFull: 1, continuation: 2 })
    }

    const focusedProgressRoot = path.join(tempRoot, "focused-diagnostic-progress")
    await mkdir(focusedProgressRoot, { recursive: true })
    await writeFile(stateFile, "0", "utf8")
    await writeFile(logFile, "", "utf8")
    process.env.SMELL_PREFLIGHT_CASE = JSON.stringify({
      name: "focused-diagnostic-progress",
      project_root: focusedProgressRoot,
      language: "cpp",
      smell: "code_clone_type1",
      location: "sample.cc:method=target|line=1",
      checkpoint_required: true,
      early_calls: 99,
      focused_status: "FAILED",
      focused_progress_each_call: true,
      max_smell_verify_cycles: 5,
      no_progress_limit: 3,
      budget: { metric: "clone_token_count", current: 9, passing_max: 24, required_reduction: 0, unit: "clone_token_count" },
    })
    const focusedProgressPlugin = await pluginModule.SmellPlugin({ worktree: focusedProgressRoot })
    const focusedProgressSession = "focused-diagnostic-progress"
    await focusedProgressPlugin["command.execute.before"](
      {
        command: "smell-refactor-run",
        sessionID: focusedProgressSession,
        arguments: `--verification-mode=project_full --max-smell-verify-cycles=5 --loop-no-progress-limit=3 -- Project root: ${focusedProgressRoot}; Language: cpp; Smell type: code_clone_type1; Target location: sample.cc:method=target|line=1`,
      },
      { parts: [] },
    )
    const focusedProgressArgs = {
      projectRoot: focusedProgressRoot,
      smell: "code_clone_type1",
      location: "sample.cc:method=target|line=1",
      verificationMode: "project_full",
    }
    const focusedProgressContext = {
      sessionID: focusedProgressSession,
      agent: "smell-refactor-agent",
      directory: focusedProgressRoot,
    }
    const focusedProgressFirst = await focusedProgressPlugin.tool.smell_verify.execute(
      focusedProgressArgs,
      focusedProgressContext,
    )
    const focusedProgressSecond = await focusedProgressPlugin.tool.smell_verify.execute(
      focusedProgressArgs,
      focusedProgressContext,
    )
    const focusedProgressFirstPayload = parseJson(
      "focused_diagnostic_progress_first",
      focusedProgressFirst.output,
    )
    const focusedProgressSecondPayload = parseJson(
      "focused_diagnostic_progress_second",
      focusedProgressSecond.output,
    )
    assertEqual("focused_diagnostic_progress_first_count", focusedProgressFirst.metadata?.command_loop_state?.no_progress_count, 0, "no_progress_count")
    assertEqual("focused_diagnostic_progress_second_count", focusedProgressSecond.metadata?.command_loop_state?.no_progress_count, 1, "no_progress_count")
    assertEqual("focused_diagnostic_progress_second_reason", focusedProgressSecondPayload.loop?.termination_reason, "", "loop.termination_reason")
    assertEqual("focused_diagnostic_progress_second_decision", focusedProgressSecondPayload.loop?.decision, "continue", "loop.decision")
    assertEqual("focused_diagnostic_progress_not_exposed_first", focusedProgressFirstPayload.focused_preflight, undefined, "focused_preflight")
    assertEqual("focused_diagnostic_progress_not_exposed_second", focusedProgressSecondPayload.focused_preflight, undefined, "focused_preflight")
    const focusedProgressCommands = (await readFile(logFile, "utf8"))
      .trim().split(/\r?\n/).filter(Boolean).map((line) => JSON.parse(line))
    assertEqual("focused_diagnostic_progress_focused_count", focusedProgressCommands.filter((item) => item.command === "focused-preflight").length, 0, "focused-preflight count")
    assertEqual("focused_diagnostic_progress_full_count", focusedProgressCommands.filter((item) => item.command === "verify").length, 0, "verify count")
    await focusedProgressPlugin.dispose?.()

    const noProgressRoot = path.join(tempRoot, "guard-progress-no-progress")
    await mkdir(noProgressRoot, { recursive: true })
    await writeFile(stateFile, "0", "utf8")
    await writeFile(logFile, "", "utf8")
    process.env.SMELL_PREFLIGHT_CASE = JSON.stringify({
      name: "no-progress",
      project_root: noProgressRoot,
      language: "c",
      smell: "nested_complexity",
      location: "sample185.c:method=target|line=1",
      checkpoint_required: true,
      early_calls: 99,
      focused_status: "READY",
      focused_progress_each_call: true,
      max_smell_verify_cycles: 5,
      no_progress_limit: 1,
      budget: { metric: "max_nesting_depth", current: 6, passing_max: 4, required_reduction: 2, unit: "max_nesting_depth" },
    })
    const noProgressPlugin = await pluginModule.SmellPlugin({ worktree: noProgressRoot })
    const noProgressSession = "guard-progress-no-progress"
    await noProgressPlugin["command.execute.before"](
      {
        command: "smell-refactor-run",
        sessionID: noProgressSession,
        arguments: `--verification-mode=project_full --max-smell-verify-cycles=5 --loop-no-progress-limit=1 -- Project root: ${noProgressRoot}; Language: c; Smell type: nested_complexity; Target location: sample185.c:method=target|line=1`,
      },
      { parts: [] },
    )
    const noProgressToolArgs = {
      projectRoot: noProgressRoot,
      smell: "nested_complexity",
      location: "sample185.c:method=target|line=1",
      verificationMode: "project_full",
    }
    const noProgressContext = {
      sessionID: noProgressSession,
      agent: "smell-refactor-agent",
      directory: noProgressRoot,
    }
    const firstNoProgress = await noProgressPlugin.tool.smell_verify.execute(noProgressToolArgs, noProgressContext)
    const firstNoProgressPayload = parseJson("guard_progress_no_progress_first", firstNoProgress.output)
    assertEqual("guard_progress_no_progress_first_decision", firstNoProgressPayload.loop?.decision, "continue", "loop.decision")
    assertEqual("guard_progress_no_progress_first_count", firstNoProgress.metadata?.command_loop_state?.no_progress_count, 0, "no_progress_count")
    process.env.SMELL_COMMAND_LOOP_STATE_JSON = JSON.stringify(firstNoProgress.metadata?.command_loop_state)
    process.env.SMELL_PROJECT_ROOT = noProgressRoot
    process.env.SMELL_LANGUAGE = "c"
    process.env.SMELL_SMELL = "nested_complexity"
    process.env.SMELL_LOCATION = "sample185.c:method=target|line=1"
    process.env.SMELL_VERIFICATION_MODE = "project_full"
    process.env.SMELL_BASELINE_SEAL = "controller-seal"
    const resumedNoProgressPlugin = await pluginModule.SmellPlugin({ worktree: noProgressRoot })
    const secondNoProgress = await resumedNoProgressPlugin.tool.smell_verify.execute(noProgressToolArgs, noProgressContext)
    const secondNoProgressPayload = parseJson("guard_progress_no_progress_second", secondNoProgress.output)
    assertEqual("guard_progress_no_progress_second_status", secondNoProgressPayload.status, "GUARD_PROGRESS_REQUIRED", "status")
    assertEqual("guard_progress_no_progress_second_decision", secondNoProgressPayload.loop?.decision, "stop", "loop.decision")
    assertEqual("guard_progress_no_progress_second_reason", secondNoProgressPayload.loop?.termination_reason, "NO_PROGRESS_LIMIT_REACHED", "loop.termination_reason")
    assertEqual("guard_progress_no_progress_second_count", secondNoProgress.metadata?.command_loop_state?.no_progress_count, 1, "no_progress_count")
    assertEqual("guard_progress_no_progress_continuation_shared", secondNoProgress.metadata?.command_loop_state?.smell_verify_cycle_count, 1, "smell_verify_cycle_count")
    const latchedNoProgress = await resumedNoProgressPlugin.tool.smell_verify.execute(
      noProgressToolArgs,
      noProgressContext,
    )
    const latchedNoProgressPayload = parseJson(
      "guard_progress_no_progress_latched",
      latchedNoProgress.output,
    )
    assertEqual("guard_progress_no_progress_latched_schema", latchedNoProgressPayload.schema_version, "smell.loop-terminal/v1", "schema_version")
    assertEqual("guard_progress_no_progress_latched_status", latchedNoProgressPayload.status, "GUARD_PROGRESS_REQUIRED", "status")
    assertEqual("guard_progress_no_progress_latched_decision", latchedNoProgressPayload.loop?.decision, "stop", "loop.decision")
    assertEqual("guard_progress_no_progress_latched_reason", latchedNoProgressPayload.loop?.termination_reason, "NO_PROGRESS_LIMIT_REACHED", "loop.termination_reason")
    assertEqual("guard_progress_no_progress_latched_count", latchedNoProgress.metadata?.command_loop_state?.no_progress_count, 1, "no_progress_count")
    const noProgressCommands = (await readFile(logFile, "utf8"))
      .trim().split(/\r?\n/).filter(Boolean).map((line) => JSON.parse(line))
    assertEqual("guard_progress_no_progress_preflight_count", noProgressCommands.filter((item) => item.command === "guard-progress").length, 2, "guard-progress count")
    assertEqual("guard_progress_no_progress_focused_count", noProgressCommands.filter((item) => item.command === "focused-preflight").length, 0, "focused-preflight count")
    assertEqual("guard_progress_no_progress_full_count", noProgressCommands.filter((item) => item.command === "verify").length, 0, "verify count")

    for (const mutationTool of [
      "edit",
      "write",
      "patch",
      "apply_patch",
      "bash",
      "task",
      "idea_refactor_apply",
      "idea_edit",
      "idea_refactor_revert_last_apply",
    ]) {
      let terminalMessage = ""
      try {
        await resumedNoProgressPlugin["tool.execute.before"](
          { tool: mutationTool, sessionID: noProgressSession },
          { args: { command: "true" } },
        )
      } catch (error) {
        terminalMessage = String(error?.message || error)
      }
      assertCond(
        `guard_progress_terminal_blocks_${mutationTool}`,
        terminalMessage.includes("SMELL_LOOP_TERMINAL"),
        `terminal command allowed ${mutationTool}`,
      )
    }
    for (const readOnlyTool of ["read", "grep", "glob", "list"]) {
      await resumedNoProgressPlugin["tool.execute.before"](
        { tool: readOnlyTool, sessionID: noProgressSession },
        { args: {} },
      )
    }
    let directIdeaPreviewMessage = ""
    try {
      await resumedNoProgressPlugin["tool.execute.before"](
        { tool: "idea_refactor_preview", sessionID: noProgressSession },
        { args: {} },
      )
    } catch (error) {
      directIdeaPreviewMessage = String(error?.message || error)
    }
    assertCond(
      "guard_progress_direct_backend_rejects_static_idea_preview",
      directIdeaPreviewMessage.includes("IDEA_BACKEND_NOT_ENABLED"),
      directIdeaPreviewMessage,
    )

    process.env.SMELL_COMMAND_LOOP_STATE_JSON = JSON.stringify(
      secondNoProgress.metadata?.command_loop_state,
    )
    const restartedTerminalPlugin = await pluginModule.SmellPlugin({ worktree: noProgressRoot })
    const restartedTerminal = await restartedTerminalPlugin.tool.smell_verify.execute(
      noProgressToolArgs,
      noProgressContext,
    )
    assertEqual(
      "guard_progress_terminal_survives_restart",
      parseJson("guard_progress_terminal_restart", restartedTerminal.output).schema_version,
      "smell.loop-terminal/v1",
      "schema_version",
    )
    await restartedTerminalPlugin["command.execute.before"](
      {
        command: "smell-refactor-run",
        sessionID: noProgressSession,
        arguments: `--verification-mode=project_full --max-smell-verify-cycles=2 --loop-no-progress-limit=1 -- Project root: ${noProgressRoot}; Language: c; Smell type: nested_complexity; Target location: sample185.c:method=target|line=1`,
      },
      { parts: [] },
    )
    await restartedTerminalPlugin["tool.execute.before"](
      { tool: "edit", sessionID: noProgressSession },
      { args: {} },
    )
    await restartedTerminalPlugin.dispose?.()

    const malformedRoot = path.join(tempRoot, "malformed-progress")
    await mkdir(malformedRoot, { recursive: true })
    await writeFile(stateFile, "0", "utf8")
    await writeFile(logFile, "", "utf8")
    process.env.SMELL_PREFLIGHT_CASE = JSON.stringify({
      name: "malformed",
      project_root: malformedRoot,
      language: "python",
      smell: "long_method",
      location: "malformed.py:method=target|line=1",
      checkpoint_required: true,
      early_calls: 0,
      malformed_progress: true,
      budget: { metric: "meaningful_line_count", current: 80, passing_max: 80, required_reduction: 0, unit: "meaningful_line_count" },
    })
    const malformedPlugin = await pluginModule.SmellPlugin({ worktree: malformedRoot })
    const malformedSession = "guard-progress-malformed"
    await malformedPlugin["command.execute.before"](
      {
        command: "smell-refactor-run",
        sessionID: malformedSession,
        arguments: `--verification-mode=project_full --max-smell-verify-cycles=2 -- Project root: ${malformedRoot}; Language: python; Smell type: long_method; Target location: malformed.py:method=target|line=1`,
      },
      { parts: [] },
    )
    const malformed = await malformedPlugin.tool.smell_verify.execute(
      {
        projectRoot: malformedRoot,
        smell: "long_method",
        location: "malformed.py:method=target|line=1",
        verificationMode: "project_full",
      },
      { sessionID: malformedSession, agent: "smell-refactor-agent", directory: malformedRoot },
    )
    const malformedPayload = parseJson("guard_progress_malformed", malformed.output)
    assertEqual("guard_progress_malformed_status", malformedPayload.status, "GUARD_PROGRESS_PROTOCOL_INVALID", "status")
    assertEqual("guard_progress_malformed_loop_terminal", malformedPayload.loop?.decision, "stop", "loop.decision")
    assertEqual("guard_progress_malformed_terminal_stage", malformed.metadata?.command_loop_state?.terminal_receipt?.stage, "protocol", "terminal stage")
    assertEqual("guard_progress_malformed_control", malformed.metadata?.command_loop_state?.control?.decision, "stop", "control decision")
    assertEqual("guard_progress_malformed_continuation", malformed.metadata?.command_loop_state?.smell_verify_cycle_count, 0, "smell_verify_cycle_count")
    const malformedCommands = (await readFile(logFile, "utf8"))
      .trim().split(/\r?\n/).filter(Boolean).map((line) => JSON.parse(line))
    assertEqual("guard_progress_malformed_preflight_count", malformedCommands.filter((item) => item.command === "guard-progress").length, 1, "guard-progress count")
    assertEqual("guard_progress_malformed_full_count", malformedCommands.filter((item) => item.command === "verify").length, 0, "verify count")

    const nonzeroProgressRoot = path.join(tempRoot, "nonzero-progress")
    await mkdir(nonzeroProgressRoot, { recursive: true })
    await writeFile(stateFile, "0", "utf8")
    await writeFile(logFile, "", "utf8")
    process.env.SMELL_PREFLIGHT_CASE = JSON.stringify({
      name: "nonzero-progress",
      project_root: nonzeroProgressRoot,
      language: "python",
      smell: "long_method",
      location: "nonzero.py:method=target|line=1",
      checkpoint_required: true,
      early_calls: 2,
      guard_progress_exit_code: 9,
      budget: { metric: "meaningful_line_count", current: 90, passing_max: 80, required_reduction: 10, unit: "meaningful_line_count" },
    })
    const nonzeroProgressPlugin = await pluginModule.SmellPlugin({ worktree: nonzeroProgressRoot })
    const nonzeroProgressSession = "guard-progress-nonzero"
    await nonzeroProgressPlugin["command.execute.before"](
      {
        command: "smell-refactor-run",
        sessionID: nonzeroProgressSession,
        arguments: `--verification-mode=project_full --max-smell-verify-cycles=2 -- Project root: ${nonzeroProgressRoot}; Language: python; Smell type: long_method; Target location: nonzero.py:method=target|line=1`,
      },
      { parts: [] },
    )
    const nonzeroProgress = await nonzeroProgressPlugin.tool.smell_verify.execute(
      {
        projectRoot: nonzeroProgressRoot,
        smell: "long_method",
        location: "nonzero.py:method=target|line=1",
        verificationMode: "project_full",
      },
      { sessionID: nonzeroProgressSession, agent: "smell-refactor-agent", directory: nonzeroProgressRoot },
    )
    const nonzeroProgressPayload = parseJson("guard_progress_nonzero", nonzeroProgress.output)
    assertEqual(
      "guard_progress_nonzero_cannot_authorize_continue",
      nonzeroProgressPayload.status,
      "GUARD_PROGRESS_PROTOCOL_INVALID",
      "status",
    )
    assertEqual("guard_progress_nonzero_is_terminal", nonzeroProgressPayload.loop?.decision, "stop", "loop.decision")
    await nonzeroProgressPlugin.dispose?.()

    for (const bypass of [
      { name: "noncheckpoint", language: "python", smell: "unsupported_smell", location: "sample.py:1", checkpoint_required: false },
    ]) {
      const bypassRoot = path.join(tempRoot, `bypass-${bypass.name}`)
      await mkdir(bypassRoot, { recursive: true })
      await writeFile(stateFile, "0", "utf8")
      await writeFile(logFile, "", "utf8")
      process.env.SMELL_PREFLIGHT_CASE = JSON.stringify({
        ...bypass,
        project_root: bypassRoot,
        early_calls: 2,
        budget: {},
      })
      const plugin = await pluginModule.SmellPlugin({ worktree: bypassRoot })
      const sessionID = `guard-progress-bypass-${bypass.name}`
      await plugin["command.execute.before"](
        {
          command: "smell-refactor-run",
          sessionID,
          arguments: `--verification-mode=project_full --max-smell-verify-cycles=2 -- Project root: ${bypassRoot}; Language: ${bypass.language}; Smell type: ${bypass.smell}; Target location: ${bypass.location}`,
        },
        { parts: [] },
      )
      const result = await plugin.tool.smell_verify.execute(
        {
          projectRoot: bypassRoot,
          smell: bypass.smell,
          location: bypass.location,
          verificationMode: "project_full",
        },
        { sessionID, agent: "smell-refactor-agent", directory: bypassRoot },
      )
      assertEqual(`guard_progress_${bypass.name}_status`, parseJson(`guard_progress_${bypass.name}`, result.output).status, "PASS", "status")
      const commands = (await readFile(logFile, "utf8"))
        .trim().split(/\r?\n/).filter(Boolean).map((line) => JSON.parse(line))
      assertEqual(`guard_progress_${bypass.name}_preflight_count`, commands.filter((item) => item.command === "guard-progress").length, 0, "guard-progress count")
      assertEqual(`guard_progress_${bypass.name}_full_count`, commands.filter((item) => item.command === "verify").length, 1, "verify count")
    }
    return {
      replayCases: results,
      candidateShellProtection: {
        projectFullLanguages: candidateShellGateResults,
        commandVariants: buildGateResults,
        compileJobCapsCovered: [1, 2],
        nonProjectFullPolicyPreserved: true,
        javaPolicyPreserved: true,
        resumedStateEnforced: true,
        resumedStateMismatchFailsClosed: true,
        malformedResumedStateFailsClosed: true,
        noncheckpointBypass: true,
        unownedBypass: true,
      },
      malformedPreflightFailsClosed: true,
      nonzeroProgressFailsClosed: true,
      javaCheckpointGate: true,
      noncheckpointBypass: true,
    }
  } finally {
    for (const key of Object.keys(process.env)) {
      if (!(key in envBefore)) delete process.env[key]
    }
    Object.assign(process.env, envBefore)
    await rm(tempRoot, { recursive: true, force: true })
  }
}

async function runManualStateDeadlineSelfCheck() {
  const tempRoot = await mkdtemp(path.join(os.tmpdir(), "smell-manual-state-self-check-"))
  const envBefore = { ...process.env }
  const realNow = Date.now
  try {
    const projectRoot = path.join(tempRoot, "project")
    const stateRoot = path.join(tempRoot, "controller-state")
    const logFile = path.join(tempRoot, "bridge-log.jsonl")
    const fakeBridge = path.join(tempRoot, "manual_bridge.py")
    await mkdir(projectRoot, { recursive: true })
    await writeFile(logFile, "", "utf8")
    await writeFile(fakeBridge, `
import json
import os
import sys
import time
from pathlib import Path

command = sys.argv[1]
state_root = Path(os.environ["SMELL_SESSION_STATE_ROOT"])
state_files = sorted((state_root / "sessions").glob("*.json")) if (state_root / "sessions").exists() else []
state_payload = json.loads(state_files[0].read_text(encoding="utf-8")) if len(state_files) == 1 else None
with Path(os.environ["SMELL_MANUAL_LOG"]).open("a", encoding="utf-8") as handle:
    handle.write(json.dumps({
        "command": command,
        "deadline": os.environ.get("SMELL_SAMPLE_DEADLINE_EPOCH_MS", ""),
        "state_file_count": len(state_files),
        "state": state_payload,
        "at_ms": int(time.time() * 1000),
    }) + "\\n")

if command == "resolve-command" and os.environ.get("SMELL_MANUAL_RESOLVE_DELAY_MS"):
    time.sleep(int(os.environ["SMELL_MANUAL_RESOLVE_DELAY_MS"]) / 1000)
if command == "resolve-command" and os.environ.get("SMELL_MANUAL_RESOLVE_FAIL") == "1":
    print(json.dumps({"success": False, "status": "RESOLVE_FAILED"}))
    raise SystemExit(17)

project_root = os.environ["SMELL_MANUAL_PROJECT_ROOT"]
identity = {
    "project_root": project_root,
    "project_override_root": "",
    "language": "python",
    "smell": "long_method",
    "location": "sample.py:method=target|line=1",
    "target_context_json": "",
    "verification_mode": "project_full",
    "sample_test_location": "",
    "sample_test_command": "",
    "build_command": "",
    "project_test_command": "",
    "verification_cwd": "",
    "verification_command_source": "",
    "sample_test_source": "",
}
if command == "resolve-command":
    payload = {
        "task": "Continue the current smell refactoring task.",
        "verification_mode": "project_full",
        "refactoring_backend": "direct",
        "allow_test_changes": False,
        "checkpoint_required": True,
        "identity": identity,
        "loop": {
            "mode": "verify-failure",
            "max_smell_verify_cycles": 2,
            "no_progress_limit": 1,
            "allowed_failure_groups": ["smell", "compile", "test"],
            "instruction": "repair narrowly",
            "sample_deadline_seconds": 60,
        },
    }
elif command == "capture-baseline":
    payload = {
        "success": True,
        "status": "BASELINE_CAPTURED",
        "baseline_seal": "manual-seal",
        "metrics": {"entity_identity": {"method": "target"}},
        "resolution_plan": {"route_family": "close-frozen-finding", "metric_budget": []},
    }
elif command == "verify" and "--guard-progress-only" in sys.argv:
    payload = {
        "schema_version": "smell.guard-progress/v1",
        "success": True,
        "status": "GUARD_PROGRESS_PASSED",
        "applicable": True,
        "checkpoint_required": True,
        "source_guard_passed": True,
        "ready_for_project_full": True,
        "project_full_executed": False,
    }
elif command == "verify":
    payload = {
        "success": True,
        "accepted": True,
        "progress": True,
        "project_full_executed": True,
        "status": "PASS",
        "resolution": "resolved",
        "smell_guard": {"success": True, "failure_count": 0, "results": []},
        "build_test_guard": {"success": True, "project_full_executed": True},
        "artifacts": {},
        "formal_verification_receipt": {
            "schema_version": "smell.formal-verification-receipt/v1",
            "terminal_stage": "formal_verify",
            "status": "PASS",
            "success": True,
            "accepted": True,
            "resolution": "resolved",
            "candidate_identity": {
                "baseline_revision": "manual-seal",
                "baseline_tree": "",
                "production_diff": "manual-production-diff",
                "test_tree": "manual-test-tree",
                "verification_config_tree": "manual-verification-config-tree",
            },
            "outcome": "pass",
            "diagnostic_signature": "PASS",
            "guard": {"success": True, "failure_count": 0},
            "build_test": {"success": True, "project_full_executed": True, "test_status": "passed"},
            "fresh_isolation": None,
            "artifact_refs": {},
        },
    }
elif command == "sleep":
    time.sleep(30)
    payload = {"success": True, "status": "PASS"}
else:
    payload = {"success": False, "status": "UNEXPECTED_COMMAND", "command": command}
print(json.dumps(payload))
`, "utf8")

    Object.assign(process.env, cleanSmellIdentityEnv(process.env), {
      SMELL_BRIDGE_FILE: fakeBridge,
      SMELL_SESSION_STATE_ROOT: stateRoot,
      SMELL_MANUAL_LOG: logFile,
      SMELL_MANUAL_PROJECT_ROOT: projectRoot,
    })
    delete process.env.SMELL_BATCH_RUN
    const compiledFile = await compilePluginForSelfCheck(tempRoot)
    const pluginModule = await import(`${pathToFileURL(compiledFile).href}?manual_state=${Date.now()}`)
    const hooks = pluginModule.SmellPlugin?.__selfTest
    assertCond("manual_state_persistence_surface", typeof hooks?.commandSessionStateFile === "function", "missing state path helper")
    assertCond("manual_deadline_process_surface", typeof hooks?.runBridge === "function", "missing deadline-aware bridge helper")

    const abortCalls = []
    const promptCalls = []
    const client = {
      session: {
        promptAsync(options) {
          promptCalls.push(options)
          return Promise.resolve({ data: undefined, error: undefined })
        },
        abort(options) {
          abortCalls.push(options)
          return Promise.resolve({ data: true, error: undefined })
        },
      },
    }
    const commandArguments = `--verification-mode=project_full --max-smell-verify-cycles=2 -- Project root: ${projectRoot}; Language: python; Smell type: long_method; Target location: sample.py:method=target|line=1`
    const sessionID = "manual-cross-process"
    const plugin = await pluginModule.SmellPlugin({ worktree: projectRoot, client })
    await plugin["command.execute.before"](
      { command: "smell-refactor-run", sessionID, arguments: commandArguments },
      { parts: [] },
    )
    const stateFile = hooks.commandSessionStateFile(sessionID)
    assertCond("manual_state_outside_candidate", !stateFile.startsWith(`${projectRoot}${path.sep}`), stateFile)
    const stateMode = (await stat(stateFile)).mode & 0o777
    const stateDirectoryMode = (await stat(path.dirname(stateFile))).mode & 0o777
    assertEqual("manual_state_file_mode", stateMode, 0o600, "mode")
    assertEqual("manual_state_directory_mode", stateDirectoryMode, 0o700, "mode")
    let envelope = JSON.parse(await readFile(stateFile, "utf8"))
    const readyInitialEnvelope = JSON.parse(JSON.stringify(envelope))
    assertEqual("manual_state_session_binding", envelope.session_id, sessionID, "session_id")
    assertEqual("manual_state_worktree_binding", envelope.worktree, path.resolve(projectRoot), "worktree")
    assertEqual("manual_state_schema_v7", envelope.command_loop_state.schema_version, 7, "schema_version")
    assertEqual("manual_state_initial_control", envelope.command_loop_state.control.decision, "verify_required", "control.decision")
    assertEqual("manual_state_command_persisted", envelope.command, "smell-refactor-run", "command")
    assertEqual("manual_state_agent_persisted", envelope.agent, "smell-refactor-agent", "agent")
    assertEqual("manual_state_initialization_ready", envelope.initialization, "ready", "initialization")
    assertEqual("manual_state_baseline_seal", envelope.baseline_seal, "manual-seal", "baseline_seal")
    assertCond("manual_state_baseline_context", envelope.command_loop_state.target_identity_context.includes("Frozen target:"), "missing baseline context")
    assertEqual(
      "manual_state_deadline_derived",
      envelope.deadline_epoch_ms,
      envelope.command_loop_state.started_at + 60_000,
      "deadline_epoch_ms",
    )
    let logEntries = (await readFile(logFile, "utf8")).trim().split(/\r?\n/).filter(Boolean).map((line) => JSON.parse(line))
    const resolveLog = logEntries.find((entry) => entry.command === "resolve-command")
    const baselineLog = logEntries.find((entry) => entry.command === "capture-baseline")
    assertCond("manual_resolve_is_deadline_bounded", Number(resolveLog?.deadline) > 0, "resolve-command lacked deadline env")
    assertEqual("manual_baseline_state_preexists", baselineLog?.state_file_count, 1, "state_file_count")
    assertEqual("manual_baseline_initial_generation", baselineLog?.state?.command_loop_state?.control?.generation, 0, "generation")
    assertEqual("manual_baseline_pending_before_capture", baselineLog?.state?.initialization, "baseline_pending", "initialization")
    assertEqual("manual_baseline_deadline_env", Number(baselineLog?.deadline), envelope.deadline_epoch_ms, "deadline env")
    await plugin.dispose?.()

    delete process.env.SMELL_COMMAND_LOOP_STATE_JSON
    delete process.env.SMELL_BASELINE_SEAL
    const reloaded = await pluginModule.SmellPlugin({ worktree: projectRoot, client })
    const systemOutput = { system: [] }
    await reloaded["experimental.chat.system.transform"]({ sessionID, model: {} }, systemOutput)
    assertCond("manual_state_cross_process_context", systemOutput.system[0]?.includes("Frozen target:"), "state did not restore")
    await reloaded.event({ event: { type: "session.idle", properties: { sessionID } } })
    await flush()
    assertEqual("manual_verify_required_rehydrated_after_restart", promptCalls.length, 1, "prompt calls")
    assertCond(
      "manual_verify_required_rehydrate_instruction",
      promptCalls[0]?.body?.parts?.[0]?.text?.includes("verify-required/initial"),
      "restart did not dispatch the persisted verify-required control",
    )
    await reloaded.event({ event: { type: "session.idle", properties: { sessionID } } })
    await flush()
    assertEqual("manual_verify_required_rehydrate_once", promptCalls.length, 1, "prompt calls")
    const verifyResult = await reloaded.tool.smell_verify.execute(
      {
        projectRoot,
        smell: "long_method",
        location: "sample.py:method=target|line=1",
        verificationMode: "project_full",
      },
      { sessionID, agent: "smell-refactor-agent", directory: projectRoot },
    )
    const verifyPayload = parseJson("manual_state_verify", verifyResult.output)
    assertEqual("manual_state_formal_pass", verifyPayload.status, "PASS", "status")
    assertEqual("manual_state_formal_stage", verifyResult.metadata?.command_loop_state?.terminal_receipt?.stage, "formal_verify", "stage")
    assertEqual("manual_state_control_stop", verifyResult.metadata?.command_loop_state?.control?.decision, "stop", "control.decision")
    assertEqual("manual_state_generation_matches_loop", verifyResult.metadata?.command_loop_state?.control?.generation, verifyPayload.loop?.generation, "generation")
    envelope = JSON.parse(await readFile(stateFile, "utf8"))
    assertEqual("manual_state_terminal_persisted", envelope.command_loop_state.terminal_receipt.stage, "formal_verify", "terminal stage")
    logEntries = (await readFile(logFile, "utf8")).trim().split(/\r?\n/).filter(Boolean).map((line) => JSON.parse(line))
    for (const entry of logEntries.filter((item) => item.command === "verify")) {
      assertEqual("manual_verify_deadline_env", Number(entry.deadline), envelope.deadline_epoch_ms, "deadline env")
    }
    const childSessionID = `${sessionID}-existing-child`
    await reloaded.event({
      event: {
        type: "session.created",
        properties: { info: { id: childSessionID, parentID: sessionID } },
      },
    })
    const lineageFile = hooks.commandSessionLineageFile(childSessionID)
    assertEqual("manual_child_lineage_persisted", existsSync(lineageFile), true, "lineage file exists")
    await reloaded.dispose?.()

    const lineageRestart = await pluginModule.SmellPlugin({ worktree: projectRoot, client })
    let childTerminalError = ""
    try {
      await lineageRestart["tool.execute.before"](
        { tool: "edit", sessionID: childSessionID },
        { args: {} },
      )
    } catch (error) {
      childTerminalError = String(error?.message || error)
    }
    assertCond(
      "manual_existing_child_terminal_survives_restart_without_created_replay",
      childTerminalError.includes("SMELL_LOOP_TERMINAL"),
      childTerminalError,
    )
    await lineageRestart.event({ event: { type: "session.deleted", properties: { info: { id: childSessionID } } } })
    await lineageRestart.event({ event: { type: "session.deleted", properties: { info: { id: sessionID } } } })
    assertEqual("manual_child_lineage_deleted_with_session", existsSync(lineageFile), false, "lineage file exists")
    assertEqual("manual_state_deleted_with_session", existsSync(stateFile), false, "state file exists")
    await lineageRestart.dispose?.()

    const confirmationSession = "manual-confirmation-restart"
    const confirmationState = hooks.restoreCommandLoopState(
      JSON.stringify(readyInitialEnvelope.command_loop_state),
    )
    assertCond("manual_confirmation_fixture_restores", Boolean(confirmationState), "initial state did not restore")
    confirmationState.control = {
      generation: 1,
      decision: "continue",
      instruction: "Do not edit the candidate; call smell_verify again for one fresh confirmation.",
      terminationReason: "",
    }
    confirmationState.smellVerifyCycleCount = 1
    confirmationState.formalCandidateState = {
      candidateIdentity: {
        baselineRevision: "manual-seal",
        baselineTree: "",
        productionDiff: "manual-production-diff",
        testTree: "manual-test-tree",
        verificationConfigTree: "manual-verification-config-tree",
      },
      outcome: "pass",
      diagnosticSignature: "PASS",
      confirmationRequired: true,
    }
    hooks.writeCommandSessionState({
      sessionID: confirmationSession,
      worktree: projectRoot,
      state: confirmationState,
      baselineSeal: "manual-seal",
      command: "smell-refactor-run",
      agent: "smell-refactor-agent",
      initialization: "ready",
    })
    const confirmationPlugin = await pluginModule.SmellPlugin({ worktree: projectRoot, client })
    const confirmationPromptStart = promptCalls.length
    const confirmationIdle = confirmationPlugin.event({
      event: { type: "session.idle", properties: { sessionID: confirmationSession } },
    })
    await confirmationPlugin.event({
      event: { type: "session.idle", properties: { sessionID: confirmationSession } },
    })
    await confirmationIdle
    await flush()
    assertEqual(
      "manual_continue_rehydrated_once_after_restart",
      promptCalls.length,
      confirmationPromptStart + 1,
      "prompt calls",
    )
    const confirmationPrompt = promptCalls.at(-1)?.body?.parts?.[0]?.text || ""
    assertCond(
      "manual_continue_rehydrates_authoritative_confirmation_instruction",
      confirmationPrompt.includes("Do not edit the candidate; call smell_verify again for one fresh confirmation.")
        && !confirmationPrompt.includes("After one narrow corrective edit"),
      confirmationPrompt,
    )
    let confirmationMutationError = ""
    try {
      await confirmationPlugin["tool.execute.before"](
        { tool: "edit", sessionID: confirmationSession },
        { args: {} },
      )
    } catch (error) {
      confirmationMutationError = String(error?.message || error)
    }
    assertCond(
      "manual_confirmation_pending_blocks_mutation_after_restart",
      confirmationMutationError.includes("SMELL_FRESH_CONFIRMATION_PENDING"),
      confirmationMutationError,
    )
    await confirmationPlugin.event({
      event: { type: "session.deleted", properties: { info: { id: confirmationSession } } },
    })
    await confirmationPlugin.dispose?.()

    const pendingSession = "manual-baseline-pending-restart"
    const pendingState = hooks.restoreCommandLoopState(
      JSON.stringify(readyInitialEnvelope.command_loop_state),
    )
    hooks.writeCommandSessionState({
      sessionID: pendingSession,
      worktree: projectRoot,
      state: pendingState,
      baselineSeal: "",
      command: "smell-refactor-run",
      agent: "smell-refactor-agent",
      initialization: "baseline_pending",
    })
    const pendingPlugin = await pluginModule.SmellPlugin({ worktree: projectRoot, client })
    const pendingPromptStart = promptCalls.length
    await pendingPlugin.event({
      event: { type: "session.idle", properties: { sessionID: pendingSession } },
    })
    await flush()
    assertEqual(
      "manual_baseline_pending_restart_does_not_dispatch_verify",
      promptCalls.length,
      pendingPromptStart,
      "prompt calls",
    )
    const pendingVerify = await pendingPlugin.tool.smell_verify.execute(
      { projectRoot, smell: "long_method", location: "sample.py:method=target|line=1", verificationMode: "project_full" },
      { sessionID: pendingSession, agent: "smell-refactor-agent", directory: projectRoot },
    )
    assertEqual(
      "manual_baseline_pending_restart_fails_closed",
      parseJson("manual_pending_terminal", pendingVerify.output).status,
      "COMMAND_INITIALIZATION_INCOMPLETE",
      "status",
    )
    await pendingPlugin.event({
      event: { type: "session.deleted", properties: { info: { id: pendingSession } } },
    })
    await pendingPlugin.dispose?.()

    const invalidStateSession = "manual-invalid-state"
    await writeFile(hooks.commandSessionStateFile(invalidStateSession), "{invalid-json\n", "utf8")
    const invalidStatePlugin = await pluginModule.SmellPlugin({ worktree: projectRoot, client })
    let invalidStateEditError = ""
    try {
      await invalidStatePlugin["tool.execute.before"](
        { tool: "edit", sessionID: invalidStateSession },
        { args: {} },
      )
    } catch (error) {
      invalidStateEditError = String(error?.message || error)
    }
    assertCond(
      "manual_invalid_state_json_blocks_edit",
      invalidStateEditError.includes("COMMAND_SESSION_STATE_INVALID"),
      invalidStateEditError,
    )
    let invalidStateVerifyError = ""
    try {
      await invalidStatePlugin.tool.smell_verify.execute(
        { projectRoot, smell: "long_method", location: "sample.py:method=target|line=1", verificationMode: "project_full" },
        { sessionID: invalidStateSession, agent: "smell-refactor-agent", directory: projectRoot },
      )
    } catch (error) {
      invalidStateVerifyError = String(error?.message || error)
    }
    assertCond(
      "manual_invalid_state_json_blocks_verify",
      invalidStateVerifyError.includes("COMMAND_SESSION_STATE_INVALID"),
      invalidStateVerifyError,
    )
    await invalidStatePlugin.event({
      event: { type: "session.deleted", properties: { info: { id: invalidStateSession } } },
    })
    await invalidStatePlugin.dispose?.()

    const invalidLineageSession = "manual-invalid-lineage"
    await writeFile(hooks.commandSessionLineageFile(invalidLineageSession), "{invalid-json\n", "utf8")
    const invalidLineagePlugin = await pluginModule.SmellPlugin({ worktree: projectRoot, client })
    let invalidLineageEditError = ""
    try {
      await invalidLineagePlugin["tool.execute.before"](
        { tool: "edit", sessionID: invalidLineageSession },
        { args: {} },
      )
    } catch (error) {
      invalidLineageEditError = String(error?.message || error)
    }
    assertCond(
      "manual_invalid_lineage_json_blocks_edit",
      invalidLineageEditError.includes("COMMAND_SESSION_LINEAGE_INVALID"),
      invalidLineageEditError,
    )
    await invalidLineagePlugin.event({
      event: { type: "session.deleted", properties: { info: { id: invalidLineageSession } } },
    })
    await invalidLineagePlugin.dispose?.()

    const replacementSession = "manual-command-replacement"
    const replacementSeedPlugin = await pluginModule.SmellPlugin({ worktree: projectRoot, client })
    await replacementSeedPlugin["command.execute.before"](
      { command: "smell-refactor-run", sessionID: replacementSession, arguments: commandArguments },
      { parts: [] },
    )
    const replacementStateFile = hooks.commandSessionStateFile(replacementSession)
    const nearDeadlineEnvelope = JSON.parse(await readFile(replacementStateFile, "utf8"))
    nearDeadlineEnvelope.command_loop_state.started_at = Date.now() - 59_000
    nearDeadlineEnvelope.deadline_epoch_ms = nearDeadlineEnvelope.command_loop_state.started_at + 60_000
    await writeFile(replacementStateFile, `${JSON.stringify(nearDeadlineEnvelope)}\n`, "utf8")
    await replacementSeedPlugin.dispose?.()

    const replacementPlugin = await pluginModule.SmellPlugin({ worktree: projectRoot, client })
    await replacementPlugin["experimental.chat.system.transform"](
      { sessionID: replacementSession, model: {} },
      { system: [] },
    )
    process.env.SMELL_MANUAL_RESOLVE_DELAY_MS = "1500"
    const replacementAbortStart = abortCalls.length
    const replacementPromptStart = promptCalls.length
    const replacementPromise = replacementPlugin["command.execute.before"](
      { command: "smell-refactor-run", sessionID: replacementSession, arguments: commandArguments },
      { parts: [] },
    )
    await new Promise((resolve) => setTimeout(resolve, 1200))
    await replacementPlugin.event({
      event: { type: "session.idle", properties: { sessionID: replacementSession } },
    })
    await flush()
    assertEqual(
      "manual_new_command_clears_old_deadline_before_resolution",
      abortCalls.length,
      replacementAbortStart,
      "abort calls",
    )
    assertEqual(
      "manual_new_command_clears_old_idle_before_resolution",
      promptCalls.length,
      replacementPromptStart,
      "prompt calls",
    )
    await replacementPromise
    delete process.env.SMELL_MANUAL_RESOLVE_DELAY_MS
    await replacementPlugin.event({
      event: { type: "session.deleted", properties: { info: { id: replacementSession } } },
    })
    await replacementPlugin.dispose?.()

    const failedReplacementSession = "manual-command-replacement-failure"
    const failedReplacementSeed = await pluginModule.SmellPlugin({ worktree: projectRoot, client })
    await failedReplacementSeed["command.execute.before"](
      { command: "smell-refactor-run", sessionID: failedReplacementSession, arguments: commandArguments },
      { parts: [] },
    )
    const failedReplacementStateFile = hooks.commandSessionStateFile(failedReplacementSession)
    const failedNearDeadlineEnvelope = JSON.parse(await readFile(failedReplacementStateFile, "utf8"))
    failedNearDeadlineEnvelope.command_loop_state.started_at = Date.now() - 59_000
    failedNearDeadlineEnvelope.deadline_epoch_ms = failedNearDeadlineEnvelope.command_loop_state.started_at + 60_000
    await writeFile(failedReplacementStateFile, `${JSON.stringify(failedNearDeadlineEnvelope)}\n`, "utf8")
    await failedReplacementSeed.dispose?.()
    const failedReplacementPlugin = await pluginModule.SmellPlugin({ worktree: projectRoot, client })
    await failedReplacementPlugin["experimental.chat.system.transform"](
      { sessionID: failedReplacementSession, model: {} },
      { system: [] },
    )
    process.env.SMELL_MANUAL_RESOLVE_FAIL = "1"
    const failedReplacementAbortStart = abortCalls.length
    const failedReplacementPromptStart = promptCalls.length
    let replacementFailure = ""
    try {
      await failedReplacementPlugin["command.execute.before"](
        { command: "smell-refactor-run", sessionID: failedReplacementSession, arguments: commandArguments },
        { parts: [] },
      )
    } catch (error) {
      replacementFailure = String(error?.message || error)
    }
    delete process.env.SMELL_MANUAL_RESOLVE_FAIL
    assertCond("manual_replacement_resolve_failure_is_reported", Boolean(replacementFailure), "resolve failure was swallowed")
    assertEqual(
      "manual_replacement_resolve_failure_removes_old_state",
      existsSync(failedReplacementStateFile),
      false,
      "state file exists",
    )
    await new Promise((resolve) => setTimeout(resolve, 1100))
    await failedReplacementPlugin.event({
      event: { type: "session.idle", properties: { sessionID: failedReplacementSession } },
    })
    await flush()
    assertEqual(
      "manual_replacement_failure_does_not_resurrect_old_deadline",
      abortCalls.length,
      failedReplacementAbortStart,
      "abort calls",
    )
    assertEqual(
      "manual_replacement_failure_does_not_resurrect_old_idle",
      promptCalls.length,
      failedReplacementPromptStart,
      "prompt calls",
    )
    await failedReplacementPlugin.dispose?.()

    const deadlineSession = "manual-deadline-expired"
    const deadlinePlugin = await pluginModule.SmellPlugin({ worktree: projectRoot, client })
    await deadlinePlugin["command.execute.before"](
      { command: "smell-refactor-run", sessionID: deadlineSession, arguments: commandArguments },
      { parts: [] },
    )
    const deadlineStateFile = hooks.commandSessionStateFile(deadlineSession)
    const deadlineEnvelope = JSON.parse(await readFile(deadlineStateFile, "utf8"))
    await deadlinePlugin.dispose?.()
    Date.now = () => deadlineEnvelope.deadline_epoch_ms + 1
    const expiredPlugin = await pluginModule.SmellPlugin({ worktree: projectRoot, client })
    await expiredPlugin["experimental.chat.system.transform"]({ sessionID: deadlineSession, model: {} }, { system: [] })
    assertEqual("manual_deadline_aborts_model_round", abortCalls.length, 1, "abort calls")
    let mutationError = ""
    try {
      await expiredPlugin["tool.execute.before"](
        { tool: "edit", sessionID: deadlineSession },
        { args: {} },
      )
    } catch (error) {
      mutationError = String(error?.message || error)
    }
    assertCond("manual_deadline_freezes_mutation", mutationError.includes("SAMPLE_DEADLINE_REACHED"), mutationError)
    const expiredVerify = await expiredPlugin.tool.smell_verify.execute(
      { projectRoot, smell: "long_method", location: "sample.py:method=target|line=1", verificationMode: "project_full" },
      { sessionID: deadlineSession, agent: "smell-refactor-agent", directory: projectRoot },
    )
    const expiredPayload = parseJson("manual_deadline_terminal", expiredVerify.output)
    assertEqual("manual_deadline_terminal_status", expiredPayload.status, "SAMPLE_DEADLINE_REACHED", "status")
    assertEqual("manual_deadline_terminal_stage", expiredPayload.stage, "protocol", "stage")
    await expiredPlugin.event({ event: { type: "session.deleted", properties: { info: { id: deadlineSession } } } })
    await expiredPlugin.dispose?.()
    Date.now = realNow

    process.env.SMELL_BATCH_RUN = "1"
    const batchSession = "batch-deadline-no-abort"
    const batchPlugin = await pluginModule.SmellPlugin({ worktree: projectRoot, client })
    await batchPlugin["command.execute.before"](
      { command: "smell-refactor-run", sessionID: batchSession, arguments: commandArguments },
      { parts: [] },
    )
    const batchEnvelope = JSON.parse(await readFile(hooks.commandSessionStateFile(batchSession), "utf8"))
    Date.now = () => batchEnvelope.deadline_epoch_ms + 1
    let batchMutationError = ""
    try {
      await batchPlugin["tool.execute.before"](
        { tool: "edit", sessionID: batchSession },
        { args: {} },
      )
    } catch (error) {
      batchMutationError = String(error?.message || error)
    }
    assertCond("batch_deadline_still_latches", batchMutationError.includes("SAMPLE_DEADLINE_REACHED"), batchMutationError)
    assertEqual("batch_deadline_does_not_abort_session", abortCalls.length, 1, "abort calls")
    await batchPlugin.event({ event: { type: "session.deleted", properties: { info: { id: batchSession } } } })
    await batchPlugin.dispose?.()
    delete process.env.SMELL_BATCH_RUN
    Date.now = realNow

    const boundedStartedAt = Date.now()
    const bounded = await hooks.runBridge(projectRoot, ["sleep"], boundedStartedAt + 100)
    assertEqual("manual_bridge_timeout_status", bounded.json?.status, "SAMPLE_DEADLINE_REACHED", "status")
    assertEqual("manual_bridge_timeout_exit", bounded.exitCode, 124, "exitCode")
    assertCond("manual_bridge_timeout_bounded", Date.now() - boundedStartedAt < 2500, "deadline termination was not bounded")
    return {
      persistence: true,
      crossProcessRestore: true,
      deadlineAbort: true,
      batchAbortSuppressed: true,
      boundedProcessTermination: true,
    }
  } finally {
    Date.now = realNow
    for (const key of Object.keys(process.env)) {
      if (!(key in envBefore)) delete process.env[key]
    }
    Object.assign(process.env, envBefore)
    await rm(tempRoot, { recursive: true, force: true })
  }
}

async function main() {
  const options = parseArgs(process.argv.slice(2))
  if (options.manualStateOnly) {
    console.log(JSON.stringify({
      success: true,
      manualState: await runManualStateDeadlineSelfCheck(),
    }, null, 2))
    return
  }
  if (options.guardProgressOnly) {
    console.log(JSON.stringify({
      success: true,
      guardProgressGate: await runGuardProgressGateSelfCheck(),
    }, null, 2))
    return
  }
  if (options.ideaProtocolOnly) {
    const tempRoot = await mkdtemp(path.join(os.tmpdir(), "idea-proposal-self-check-"))
    try {
      const skillDocs = await runIdeaSkillProtocolDocSelfCheck()
      const compiledFile = await compilePluginForSelfCheck(tempRoot)
      const pluginModule = await import(`${pathToFileURL(compiledFile).href}?idea_protocol=${Date.now()}`)
      const backendSurface = await runIdeaBackendSurfaceSelfCheck(pluginModule)
      const result = await runPluginNormalizeSelfCheck(pluginModule)
      const manualCommandProtocol = await runIdeaManualCommandProtocolSelfCheck()
      console.log(JSON.stringify({
        success: true,
        node: process.version,
        ideaSkillDocs: skillDocs,
        ideaBackendSurface: backendSurface,
        ideaProposalProtocol: result.ideaProposalResults,
        ideaManualCommandProtocol: manualCommandProtocol,
      }, null, 2))
      return
    } finally {
      await rm(tempRoot, { recursive: true, force: true })
    }
  }
  const fixtureRoot = await makeFixtureProject()
  const artifactRoot = await mkdtemp(path.join(os.tmpdir(), "smell-verify-self-check-artifacts-"))
  try {
    const ideaSkillDocs = await runIdeaSkillProtocolDocSelfCheck()
    const bridge = await runBridgeSelfCheck(fixtureRoot, artifactRoot)
    const pluginSelfCheck = await runPluginSelfCheck(fixtureRoot, artifactRoot)
    const guardProgressGate = await runGuardProgressGateSelfCheck()
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
      ideaSkillDocs,
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
      guardProgressGate,
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

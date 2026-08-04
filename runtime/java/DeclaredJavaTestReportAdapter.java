import java.io.IOException;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.List;

/**
 * Runs explicitly declared JUnit 4 test classes and writes one JUnit-style XML
 * report per class. The class intentionally has no compile-time dependency on
 * JUnit. Main-based test programs are executed as separate JVM processes by
 * the Python attestation runner so their process semantics remain unchanged.
 */
public final class DeclaredJavaTestReportAdapter {
    private DeclaredJavaTestReportAdapter() {
    }

    public static void main(String[] args) throws Exception {
        Arguments parsed = Arguments.parse(args);
        Files.createDirectories(parsed.reportDir);
        boolean success = true;
        for (String className : parsed.classes) {
            Result result;
            try {
                result = runJUnit4(className);
            } catch (Throwable failure) {
                Throwable cause = unwrap(failure);
                cause.printStackTrace(System.err);
                result = new Result(1, 0, 1, cause.toString());
            }
            writeReport(parsed.reportDir, className, parsed.mode, result);
            System.out.println(
                    "DECLARED_TEST_REPORT class=" + className
                            + " tests=" + result.tests
                            + " skipped=" + result.skipped
                            + " failures=" + result.failures);
            if (result.tests - result.skipped <= 0 || result.failures > 0) {
                success = false;
            }
        }
        if (!success) {
            System.exit(1);
        }
    }

    private static Result runJUnit4(String className) throws Exception {
        Class<?> testClass = Class.forName(className);
        Class<?> junitCore = Class.forName("org.junit.runner.JUnitCore");
        Method runClasses = junitCore.getMethod("runClasses", Class[].class);
        Object result = runClasses.invoke(null, (Object) new Class<?>[]{testClass});
        Class<?> resultClass = Class.forName("org.junit.runner.Result");
        int executed = (Integer) resultClass.getMethod("getRunCount").invoke(result);
        int skipped = (Integer) resultClass.getMethod("getIgnoreCount").invoke(result);
        int failures = (Integer) resultClass.getMethod("getFailureCount").invoke(result);
        @SuppressWarnings("unchecked")
        List<Object> failureList = (List<Object>) resultClass.getMethod("getFailures").invoke(result);
        StringBuilder details = new StringBuilder();
        for (Object failure : failureList) {
            if (details.length() > 0) {
                details.append('\n');
            }
            details.append(String.valueOf(failure));
        }
        return new Result(executed + skipped, skipped, failures, details.toString());
    }

    private static Throwable unwrap(Throwable failure) {
        if (failure instanceof InvocationTargetException
                && ((InvocationTargetException) failure).getCause() != null) {
            return ((InvocationTargetException) failure).getCause();
        }
        return failure;
    }

    private static void writeReport(
            Path reportDir,
            String className,
            String mode,
            Result result) throws IOException {
        String reportName = "TEST-" + className.replace('$', '_') + ".xml";
        String failure = result.failures > 0
                ? "<failure message=\"declared test failed\">"
                        + xml(result.details) + "</failure>"
                : "";
        String testCase = "<testcase classname=\"" + xml(className)
                + "\" name=\"" + xml(mode) + "\">" + failure + "</testcase>";
        String xml = "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
                + "<testsuite name=\"" + xml(className) + "\" tests=\""
                + result.tests + "\" failures=\"" + result.failures
                + "\" errors=\"0\" skipped=\"" + result.skipped + "\">"
                + testCase + "</testsuite>\n";
        Files.write(reportDir.resolve(reportName), xml.getBytes(StandardCharsets.UTF_8));
    }

    private static String xml(String value) {
        return value.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace("\"", "&quot;")
                .replace("'", "&apos;");
    }

    private static final class Result {
        private final int tests;
        private final int skipped;
        private final int failures;
        private final String details;

        private Result(int tests, int skipped, int failures, String details) {
            this.tests = tests;
            this.skipped = skipped;
            this.failures = failures;
            this.details = details;
        }
    }

    private static final class Arguments {
        private final Path reportDir;
        private final String mode;
        private final List<String> classes;

        private Arguments(Path reportDir, String mode, List<String> classes) {
            this.reportDir = reportDir;
            this.mode = mode;
            this.classes = classes;
        }

        private static Arguments parse(String[] args) {
            Path reportDir = null;
            String mode = "";
            List<String> classes = new ArrayList<String>();
            for (int index = 0; index < args.length; index++) {
                if ("--report-dir".equals(args[index]) && index + 1 < args.length) {
                    reportDir = Paths.get(args[++index]);
                } else if ("--mode".equals(args[index]) && index + 1 < args.length) {
                    mode = args[++index];
                } else {
                    classes.add(args[index]);
                }
            }
            if (reportDir == null
                    || !"junit4".equals(mode)
                    || classes.isEmpty()) {
                throw new IllegalArgumentException(
                        "usage: --report-dir PATH --mode junit4 CLASS...");
            }
            return new Arguments(reportDir, mode, classes);
        }
    }
}

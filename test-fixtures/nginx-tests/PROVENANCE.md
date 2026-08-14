# Pinned nginx test fixture

This is a bounded subset of the official `nginx/nginx-tests` suite, pinned at
commit `1502b87f5fa712ff485a1bb6baeab50153719d03` (2026-05-05). The pin predates
the delivery image's nginx commit by one day, avoiding tests for newer nginx
features while retaining real behavioral coverage of access, autoindex, body,
configuration dump, empty GIF, map, rewrite, and split-clients modules.

The original BSD-2-Clause license is included as `LICENSE`. The runner requires
exactly the eight selected test files and their Perl helper; there is no
smoke-test fallback.

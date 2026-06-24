"""Process exit-code policy.

0 = clean (or below threshold)
1 = config / tool error
2 = lint findings at/above fail-on (regressions only when a baseline is set)
3 = connection / attach error
"""

from __future__ import annotations

EXIT_OK = 0
EXIT_TOOL_ERROR = 1
EXIT_FINDINGS = 2
EXIT_CONNECTION = 3


def exit_code_for(report) -> int:
    return EXIT_OK if report.passed() else EXIT_FINDINGS

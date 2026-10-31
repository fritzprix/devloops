# Producer Tasks Reference

The producer loop runs periodic audits across seven specialized areas to detect code defects, regressions, security risks, and technical debt.

Projects can map each task to an existing project skill or command via `producer.task_skills` in `devloops.config.yaml`. If unmapped, generic checks apply.

---

## 1. `security`

Audits codebase and dependency manifests for security vulnerabilities, exposed credentials, and dangerous configurations.

- **Inputs**: Source code, lockfiles (`package-lock.json`, `poetry.lock`, `Cargo.lock`, etc.), configuration files, container/CI files.
- **Checks**:
  - Hardcoded secrets, API keys, private tokens, internal endpoints.
  - Known CVEs in dependencies.
  - Insecure deserialization, SQL/command injection patterns, improper input validation.
  - Permissive CORS, insecure cookie flags, disabled authentication middleware.
- **Fingerprint key**: `f"security:{rule_id}:{file_path}:{identifier}"` (e.g. `security:cwe-798:src/auth.py:SECRET_KEY`).
- **Skill reuse**: Maps to `producer.task_skills.security` (e.g. `code-audit-expert` or static scanner). If unmapped, performs rule-based pattern audits for credentials and common CWE patterns.

---

## 2. `e2e`

Verifies end-to-end user workflows, integration endpoints, and subsystem interactions.

- **Inputs**: Test directories, API specs, scenario configurations, scratch directory (`.devloops/scratch` or temporary work directory).
- **Checks**:
  - Broken end-to-end user journeys or test failures.
  - API contract drift or breaking payload discrepancies.
  - Unhandled integration timeouts or service disconnect failures.
- **Fingerprint key**: `f"e2e:{suite_name}:{scenario_name}"` (e.g. `e2e:checkout_flow:payment_timeout`).
- **Skill reuse**: Maps to `producer.task_skills.e2e` (e.g. `test-runner`). If unmapped, executes project integration test commands. Unsafe operations (such as running test servers or modifying databases) must execute strictly in a dedicated scratch directory.

---

## 3. `uiux`

Audits user interface consistency, responsive layouts, accessibility, and client-side error states.

- **Inputs**: UI templates, components, stylesheets, accessibility tags, translation files.
- **Checks**:
  - Missing ARIA labels, missing image alt attributes, improper keyboard focus trapping.
  - Missing loading indicators, broken responsive layout rules.
  - Unhandled UI error states or untrapped frontend exceptions.
- **Fingerprint key**: `f"uiux:{component_name}:{defect_type}"` (e.g. `uiux:LoginForm:missing_aria_describedby`).
- **Skill reuse**: Maps to `producer.task_skills.uiux` (e.g. `ui-auditor`). If unmapped, inspects component hierarchies for accessibility attributes and error boundaries.

---

## 4. `regression`

Analyzes recent changes and test coverage to detect unintended behavioral regressions and untested edge conditions.

- **Inputs**: Git commit log on `project.base_branch`, recent diffs, test suite coverage reports.
- **Checks**:
  - Modified function signatures lacking regression test coverage.
  - Edge conditions introduced by recent bug fixes that lack regression guards.
  - Re-emergence of known bug patterns previously fixed in history.
- **Fingerprint key**: `f"regression:{module_name}:{issue_signature}"` (e.g. `regression:parser:unicode_normalization_bypass`).
- **Skill reuse**: Maps to `producer.task_skills.regression`. If unmapped, inspects recent commits on the base branch and verifies test coverage around altered code branches.

---

## 5. `usage-logs`

Audits runtime execution logs, telemetry streams, and error aggregations for recurring anomalies.

- **Inputs**: Local log files, telemetry output, debug reports, application stderr.
- **Checks**:
  - Recurring uncaught exceptions or error logs.
  - Rapid warning spam indicating resource exhaustion or deprecated API calls.
  - Unusually slow operation warnings or timeout alerts.
- **Fingerprint key**: `f"usage-logs:{logger_name}:{error_signature}"` (e.g. `usage-logs:database:connection_pool_starvation`).
- **Skill reuse**: Maps to `producer.task_skills.usage-logs`. If unmapped, parses log outputs for stack traces and high-frequency warning signatures.

---

## 6. `doc-drift`

Detects discrepancies between implementation code and external/internal documentation.

- **Inputs**: Documentation files (`README.md`, `docs/`, OpenAPI specs), CLI flags, exported function signatures.
- **Checks**:
  - Documented CLI arguments or configuration parameters that do not exist in code.
  - Outdated installation steps, missing environment variable documentation.
  - Broken internal links or outdated architectural diagrams.
- **Fingerprint key**: `f"doc-drift:{doc_file}:{symbol_or_flag}"` (e.g. `doc-drift:README.md:--enable-cache`).
- **Skill reuse**: Maps to `producer.task_skills.doc-drift`. If unmapped, verifies public CLI options and config keys against references in docs.

---

## 7. `cleanup`

Proposes cleanups for dead branches, obsolete build artifacts, and abandoned assets.

- **Inputs**: Git branch lists (`git branch -r`), worktree lists, asset directories.
- **Checks**:
  - Stale merged or abandoned feature branches (older than threshold).
  - Obsolete asset files and unused static fixtures.
  - Abandoned worktrees and cache files.
- **Fingerprint key**: `f"cleanup:{target_type}:{target_identifier}"` (e.g. `cleanup:stale_branch:feat/old-prototype`).
- **Skill reuse**: Maps to `producer.task_skills.cleanup`. If unmapped, identifies stale remote branches and dangling worktrees.
- **Safety rules**:
  - Proposes cleanups in **one rolling issue per week**.
  - **Never deletes branches or closes issues automatically**.
  - **Never proposes deleting base/default branches** or branches listed in `cleanup.protected_branches`.

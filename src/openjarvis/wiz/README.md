# Wiz: Autonomous Feature Engineering System

Wiz is an autonomous system for end-to-end feature implementation and deployment for Wize. It coordinates:

```
FeatureRequest → Plan → Implement → Test → Risk Assessment → PR → Review → Merge → Deploy → Verify → Complete
```

All operations require **real evidence** before proceeding. Unknown/missing/timeout states result in **failure-closed** behavior.

## Architecture

### Core Components

- **FeatureOrchestrator**: Main orchestration engine
- **GitHubClient**: Real GitHub REST API (no MCP dependency)
- **VercelClient**: Real Vercel deployment verification
- **ClaudeExecutor**: Claude Code integration for implementation
- **RiskAssessor**: Deterministic risk classification from diffs

### Pipeline Stages

1. **PLANNED**: Validate request, design scope
2. **IMPLEMENTING**: Generate/apply code changes (Claude or manual)
3. **TESTING**: Run test suite, verify all pass
4. **RISKING**: Assess final risk from diff (initial guess is overridden)
5. **PULL_REQUEST**: Create PR with autonomous assessment
6. **REVIEWING**: Independent review (advisory, doesn't block)
7. **MERGING**: Apply deterministic merge gates
8. **DEPLOYING**: Wait for Vercel Preview, verify exact SHA
9. **VERIFYING**: Run production acceptance tests
10. **COMPLETE**: Feature live and verified

## Setup

### 1. Configure GitHub API Access

Create `~/.config/openjarvis/connectors/github.json`:

```json
{
  "token": "ghp_YOUR_GITHUB_PAT"
}
```

Token must have scopes:
- `repo` (full repository access)
- `workflow` (GitHub Actions)
- `read:org` (organization access for merge checks)

### 2. Configure Vercel API Access

Create `~/.config/openjarvis/connectors/vercel.json`:

```json
{
  "token": "your_vercel_api_token"
}
```

Get token from: https://vercel.com/account/tokens

### 3. Set Up Target Repository

The target Wize repository must:
- Have Vercel connected for automatic deployments
- Have branch protection on `main` (optional but recommended)
- Have test suite that runs in CI
- Be accessible by the GitHub account with your PAT

### 4. Verify Claude CLI

```bash
claude --version  # Should print version
claude -p "test"  # Should output: test (in non-interactive mode)
```

## Usage

```python
from openjarvis.wiz.core import FeatureRequest, RiskLevel
from openjarvis.wiz.orchestrator import FeatureOrchestrator

# Create a feature request
request = FeatureRequest(
    owner="user@example.com",
    feature="Add dark mode toggle to dashboard",
    repository="axe11112/Wize",
    base_branch="main",
    acceptance_criteria=[
        "Toggle button visible on dashboard",
        "Dark mode CSS applies correctly",
        "Settings persist across sessions",
    ],
    constraints=[
        "no_auth_changes",
        "no_database_schema_changes",
    ],
)

# Process through pipeline
orchestrator = FeatureOrchestrator(
    repo_owner="axe11112",
    repo_name="Wize",
    repo_path="/path/to/Wize",
)

result = orchestrator.process_request(request)

if result.state == "COMPLETE":
    print(f"✓ Feature live in production")
    print(f"  PR: #{result.pr_number}")
    print(f"  Merge SHA: {result.merge_sha}")
    print(f"  Production SHA: {result.production_sha}")
else:
    print(f"✗ Feature failed: {result.failure_reason}")
```

## Design Principles

### 1. Fail-Closed

- UNKNOWN risk → refuse merge
- Missing evidence → refuse action
- Timeout → treat as failure
- Unverified SHAs → refuse deployment

### 2. Real Evidence Only

- Real GitHub API (not MCP mocking)
- Real test execution (not stubbed)
- Real Vercel deployments (not simulated)
- Real production verification

### 3. Deterministic Gates

Merge authority by risk level:

| Risk | Authority | Auto-merge? |
|------|-----------|-------------|
| LOW | Autonomous | Yes, if all gates pass |
| MEDIUM | Operator approval | No |
| HIGH | Owner approval | No |
| UNKNOWN | Refuse | No |

### 4. TOCTOU Protection

Before merge, re-check:
- PR still open
- No head changes
- All status checks still pass
- Vercel still working
- No new production incidents

## Merge Gates (All Must Pass for Autonomous LOW)

- ✓ Feature request valid and within scope
- ✓ Repository and branches exist
- ✓ Feature branch contains only expected changes
- ✓ Final risk is LOW (not UNKNOWN)
- ✓ Test suite passes (0 failures)
- ✓ Lint/typecheck/build succeeds
- ✓ No secrets found
- ✓ No security violations
- ✓ PR created and mergeable
- ✓ Independent review complete
- ✓ Vercel Preview ready
- ✓ Preview SHA exactly matches PR HEAD
- ✓ Acceptance tests pass on Preview
- ✓ No emergency stop active
- ✓ No blocking production incidents

## Risk Assessment

Risk is determined AFTER code changes are applied, not before.

### LOW Risk Conditions
- UI/frontend changes only (tsx, css, markdown)
- ≤ 5 small files changed
- No dangerous patterns (auth, billing, database, schema, RLS)
- Passes all tests
- No security findings

### MEDIUM Risk
- Multi-file changes
- Some infrastructure changes
- Requires operator approval

### HIGH Risk
- Authentication/authorization changes
- Database migrations or schema changes
- Billing/payment logic
- Production configuration
- Requires owner approval

### UNKNOWN
- Cannot determine risk (missing diff, git error)
- Refuses to proceed

## Feature Implementation

Wiz can be triggered by:

1. **Manual FeatureRequest**: Python API
2. **Owner commands** (future): Slack/Telegram/email interface
3. **Scheduled proposals** (future): AI-suggested improvements

Currently, feature implementation requires:
- Manual code changes (applied to feature branch), OR
- Claude Code integration (via `claude -p` non-interactive mode)

Wiz validates and verifies, not implements directly.

## Testing

Run all Wiz tests:

```bash
uv run pytest tests/wiz/ -v
```

Expected: 34+ tests pass

Tests use:
- Mocked GitHub/Vercel APIs (don't require credentials)
- Temporary git repositories
- Deterministic scenarios

Real integration tests (requires credentials):
```bash
# Set up credentials first
uv run pytest tests/wiz/test_github_client.py::test_real_github_integration -v
uv run pytest tests/wiz/test_vercel_client.py::test_real_vercel_integration -v
```

## Limitations & Future Work

### Current

- Feature implementation must be done externally (Claude Code or manual)
- No automated feature planning yet
- No automated acceptance test generation
- No rollback automation
- Limited incident detection

### Planned (Phase 2)

- Full Claude integration for autonomous code generation
- Feature planning based on issue descriptions
- Automatic acceptance test generation from criteria
- Production health monitoring
- Automatic incident response
- Rollback detection and prevention

## Production Deployments

Once a feature merges to `main`:

1. Vercel automatically builds/deploys
2. Wiz monitors deployment status
3. Vercel SHA must exactly match merge SHA
4. Production acceptance tests run
5. Feature marked COMPLETE only after:
   - Deployment successful
   - SHA verified
   - Acceptance tests pass

## Safety Guarantees

✓ **No blind merges**: Every gate must pass with real evidence
✓ **No stale deployments**: SHA verification prevents old code going live
✓ **No unverified changes**: Diffs must be small and assessed
✓ **No ignored test failures**: Tests must pass or merge is blocked
✓ **No secret leaks**: Secret scanning before merge
✓ **No escalation**: Only approved risk levels can auto-merge

## Troubleshooting

### GitHub credentials not found

```bash
mkdir -p ~/.config/openjarvis/connectors
echo '{"token": "ghp_..."}' > ~/.config/openjarvis/connectors/github.json
chmod 600 ~/.config/openjarvis/connectors/github.json
```

### Vercel deployment not found

- Verify project is linked to Vercel
- Check Vercel tokens with: `curl -H "Authorization: Bearer $TOKEN" https://api.vercel.com/v1/projects`
- Ensure branch protection is configured correctly

### Tests failing

- Run with verbose output: `pytest tests/wiz/ -vv`
- Check git is configured: `git config user.name "Test" && git config user.email "test@test.com"`
- Verify temporary paths are writable

## Architecture Decisions

### Why Real APIs, Not MCP?

- Wiz must operate autonomously outside Claude Code sessions
- MCP tools are only available in interactive Claude sessions
- Real APIs enable standalone CLI operation
- Real APIs provide better error handling and retry logic

### Why Fail-Closed?

- Unknown state is dangerous for autonomous systems
- Missing evidence could hide problems
- Better to ask a human than assume success
- Production safety is paramount

### Why No Blind Automation?

- Each stage produces evidence that gate checks
- No "assume it worked" behavior
- Deterministic decision points based on measurable facts
- Audit trail of every decision

## Related Reading

- [OpenJarvis Agents](../agents/README.md)
- [Reliability Framework](../reliability/README.md) - Production safety primitives
- [Claude Code Integration](../../cli/README.md)

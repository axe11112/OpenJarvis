# Wiz: Autonomous Wize Engineering System

Wiz is an autonomous engineering and operations system for the Wize Performance application. It can monitor, repair, develop, test, and operate Wize with minimal human involvement.

## Goal

Enable an owner to tell Wiz to:

```
"Fix the website."
"Build dark mode."
"Improve the dashboard."
"Make onboarding better."
"Why is login slow?"
"Build this feature: ..."
```

And have Wiz handle almost everything itself.

## Architecture

Wiz operates through a deterministic feature engineering pipeline:

### Pipeline Stages

1. **Request Processing** (`dispatcher`) - Parse owner request and create typed `FeatureRequest` with preliminary risk assessment
2. **Planning** (`orchestrator`) - Determine implementation strategy
3. **Implementation** (`claude_session`) - Spawn Claude Code session to implement changes
4. **Repository** (`repository`) - Manage git branches, commits, and push
5. **Testing** (`testing`) - Run unit, integration, and acceptance tests
6. **Verification** (`verification`) - Deploy to Vercel Preview and verify
7. **Code Review** (`review`) - Independent code review with findings
8. **Merge Gates** (`merge_gates`) - Deterministic checks before merge
9. **Merge** (`github_integration`) - Create/manage GitHub PRs and merge
10. **Notifications** (`notifications`) - Notify owner of progress/completion

### Safety Principles

- **UNKNOWN risk** → REJECT (cannot proceed)
- **LOW risk** → Can merge autonomously if all gates pass
- **MEDIUM/HIGH risk** → Requires human approval
- **Blocking findings** → Cannot merge
- **Failed tests** → Cannot merge
- **Unverified Preview** → Cannot merge

### Core Components

#### Models (`models.py`)
- `FeatureRequest` - Tracks a feature through the pipeline
- `FeatureState` - States: created, planned, implementing, testing, previewing, reviewing, approved_for_merge, merged, deployed_to_production, complete, failed, blocked, requires_human
- `RiskLevel` - LOW, MEDIUM, HIGH, UNKNOWN

#### Dispatcher (`dispatcher/`)
- `RequestDispatcher` - Transform natural language requests into structured `FeatureRequest` with preliminary risk classification

#### Repository (`repository/`)
- `RepositoryManager` - Git operations: create feature branches, get diffs, push changes

#### Orchestrator (`orchestrator/`)
- `WizOrchestrator` - Coordinate entire feature engineering pipeline

#### Safety (`safety/`)
- `SafetyGates` - Deterministic gates enforcing autonomous shipping rules

#### Testing (`testing/`)
- `TestRunner` - Run unit, integration, acceptance, and production tests
- `AcceptanceTestGenerator` - Generate tests from feature description

#### Verification (`verification/`)
- `VercelPreviewManager` - Manage Vercel Preview deployments
- `ProductionVerificationExecutor` - Verify feature in production

#### Review (`review.py`)
- `IndependentReviewer` - Perform code review
- `CodeReviewFinding` - Track issues (security, performance, testing, etc.)

#### Merge Gates (`merge_gates.py`)
- `MergeGates` - Deterministic checks before autonomous merge

#### GitHub (`github_integration.py`)
- `GitHubIntegration` - Manage pull requests and merging

#### Notifications (`notifications/`)
- `NotificationManager` - Notify owner via Telegram, Email, Slack

### Autonomy Rules

**Autonomous (no human required):**
- LOW risk features with all gates passing

**Human Approval Required:**
- MEDIUM or HIGH risk features
- Blocking review findings
- Failed tests
- UNKNOWN risk classification

**Never Autonomous:**
- Authentication/Authorization changes
- Payment/Billing changes
- Database schema migrations
- Permission/RLS changes
- Security control modifications
- Destructive operations
- Infrastructure changes
- CI/CD policy changes

## Usage

```bash
# Submit a feature request
wiz feature "Add dark mode to dashboard"

# Check feature status
wiz status WIZE-abc123

# List active features
wiz list-features

# Check Wiz health
wiz health
```

## Test Coverage

56 comprehensive tests covering:
- Core models and state transitions
- Request dispatcher with risk classification
- Safety gates (LOW/MEDIUM/HIGH/UNKNOWN logic)
- Merge gates (comprehensive pre-merge checks)
- Code review and severity levels
- GitHub PR management
- Notifications
- Architecture constraints (Wiz ≠ Reliability)
- End-to-end integration scenarios

## Next Steps

High-priority work to complete Wiz:

1. **Real Vercel Integration** - Replace placeholders with actual Vercel API calls
2. **Real GitHub Integration** - Replace placeholders with actual GitHub API
3. **Claude Code Session Spawning** - Actually spawn Claude CLI sessions for implementation
4. **Test Execution** - Real test running against repositories
5. **Production Verification** - Real HTTP/browser probes for production health
6. **Independent Review** - AI-driven code review using Claude
7. **Telegram Notifications** - Real owner notifications
8. **Control Center** - Dashboard showing Wiz status and active work

## Architecture Constraints

- **Wiz is independent of Reliability** - Wiz may use reliability services, but reliability never depends on Wiz
- **One-way dependencies** - Clear separation between systems
- **Fail-closed** - UNKNOWN status means refuse progression
- **Real evidence only** - No fabricated success signals
- **Deterministic gates** - All shipping decisions are testable and reproducible

## Philosophy

> "The goal tonight is not a beautiful report. The goal is that when I wake up, Wiz is materially closer to being able to operate and continuously develop Wize without me."

Every component is built with real autonomy in mind, not just the illusion of it. Wiz succeeds only when it demonstrates capability through real, verified execution.

# Wiz Autonomous Feature System Setup Guide

This guide enables you to run the complete Wiz autonomous feature pipeline with real GitHub and Vercel integrations.

## Prerequisites

- Python 3.10+
- `uv` package manager  
- Git installed and configured
- Claude CLI installed (`claude --version` should work)
- OpenJarvis repository cloned

## Step 1: Install Dependencies

```bash
cd /path/to/OpenJarvis
uv pip install -e ".[dev]"
```

Verify installation:
```bash
uv run pytest tests/wiz/ -v
# Should show: 34 passed
```

## Step 2: Configure GitHub API

### Get GitHub Personal Access Token

1. Go to https://github.com/settings/tokens
2. Create new token (Personal Access Tokens → Fine-grained tokens)
3. Name: `Wiz Autonomous System`
4. Permissions needed:
   - Repository access: Select your target repo(s)
   - Permissions:
     - `Contents` (read+write) - for commits and branches
     - `Pull Requests` (read+write) - for PR creation/merging
     - `Workflows` (read) - to check CI status
     - `Administration` (read) - for branch protection checks
5. Copy the token (appears once)

### Store Token

```bash
mkdir -p ~/.config/openjarvis/connectors
cat > ~/.config/openjarvis/connectors/github.json << 'EOF'
{
  "token": "ghp_YOUR_TOKEN_HERE"
}
EOF
chmod 600 ~/.config/openjarvis/connectors/github.json
```

### Test Access

```bash
uv run python -c "
from openjarvis.wiz.github_client import GitHubClient
client = GitHubClient()
user = client.get_user()
print(f'✓ Connected as {user[\"login\"]}')
"
```

Expected output: `✓ Connected as your_github_username`

## Step 3: Configure Vercel API

### Get Vercel API Token

1. Go to https://vercel.com/account/tokens
2. Create new token
3. Name: `Wiz Autonomous System`
4. Scope: Select your Wize project
5. Copy the token

### Store Token

```bash
cat > ~/.config/openjarvis/connectors/vercel.json << 'EOF'
{
  "token": "YOUR_VERCEL_TOKEN"
}
EOF
chmod 600 ~/.config/openjarvis/connectors/vercel.json
```

### Test Access

```bash
uv run python -c "
from openjarvis.wiz.vercel_client import VercelClient
client = VercelClient()
# If no error, token is working
print('✓ Vercel client configured')
"
```

## Step 4: Set Up Target Repository

Choose or create a test repository (e.g., a fork of your main project):

```bash
export WIZE_OWNER="your-github-username"
export WIZE_REPO="Wize"  # or test-wize
export WIZE_PATH="/path/to/local/clone"
```

Clone it locally:
```bash
git clone https://github.com/$WIZE_OWNER/$WIZE_REPO $WIZE_PATH
cd $WIZE_PATH
git config user.name "Wiz"
git config user.email "wiz@autonomousai.dev"
```

### Link to Vercel (if not already)

For Wiz to track deployments, the repository must be connected to Vercel:

1. Go to https://vercel.com/new
2. Import your GitHub repository
3. Select project settings
4. Note the project name/ID (for later)

## Step 5: Verify Setup

Run the verification script:

```bash
uv run python -c "
from openjarvis.wiz.github_client import GitHubClient
from openjarvis.wiz.vercel_client import VercelClient
from openjarvis.wiz.orchestrator import FeatureOrchestrator

print('✓ GitHub client:', 'configured' if GitHubClient().is_configured else 'MISSING')
print('✓ Vercel client:', 'configured' if VercelClient().is_configured else 'MISSING')
print('✓ Orchestrator: available')
print()
print('All systems ready for Wiz autonomous operation!')
"
```

Expected output:
```
✓ GitHub client: configured
✓ Vercel client: configured
✓ Orchestrator: available

All systems ready for Wiz autonomous operation!
```

## Step 6: Run a Pilot Feature

### Simple Example: Add a UI Button

```bash
cd /path/to/OpenJarvis
uv run python << 'EOF'
from openjarvis.wiz.core import FeatureRequest, RiskLevel
from openjarvis.wiz.orchestrator import FeatureOrchestrator

# Define the feature
request = FeatureRequest(
    owner="test@example.com",
    feature="Add a hello world button to the dashboard",
    repository="your-github-username/Wize",
    base_branch="main",
    acceptance_criteria=[
        "Button appears on dashboard",
        "Button text is 'Say Hello'",
        "Clicking button shows alert",
    ],
    constraints=[
        "frontend_only",
        "no_auth_changes",
        "no_database_changes",
    ],
    estimated_risk=RiskLevel.LOW,
)

# Process the request
orchestrator = FeatureOrchestrator(
    repo_owner="your-github-username",
    repo_name="Wize",
    repo_path="/path/to/local/Wize",
)

# Note: This will fail at the implementation step because we haven't
# actually written code. This demonstrates the pipeline structure.
try:
    result = orchestrator.process_request(request)
    print(f"Status: {result.state}")
    if result.pr_number:
        print(f"PR: #{result.pr_number}")
except Exception as e:
    print(f"Expected error (implementation step): {e}")
EOF
```

### Manual Feature (for testing without Claude)

If you want to manually apply changes and let Wiz handle the rest:

1. Create feature branch:
```bash
cd $WIZE_PATH
git checkout -b wiz/test-feature
```

2. Make a small change (e.g., edit README):
```bash
echo "Test change by Wiz" >> README.md
git add README.md
git commit -m "test: add hello world button"
git push -u origin wiz/test-feature
```

3. Run the orchestrator to create PR and test merge gates:
```bash
uv run python << 'EOF'
from openjarvis.wiz.core import FeatureRequest, RiskLevel
from openjarvis.wiz.orchestrator import FeatureOrchestrator

request = FeatureRequest(
    owner="test@example.com",
    feature="Test feature for pipeline validation",
    repository="your-github-username/Wize",
    feature_branch="wiz/test-feature",
    base_branch="main",
    acceptance_criteria=["README updated"],
    estimated_risk=RiskLevel.LOW,
)

orchestrator = FeatureOrchestrator(
    repo_owner="your-github-username",
    repo_name="Wize",
    repo_path="/path/to/local/Wize",
)

result = orchestrator.process_request(request)
print(f"Result: {result.state}")
print(f"PR: {result.pr_url}")
EOF
```

## Troubleshooting

### GitHub API Error: 404 Not Found

- Token may be invalid or expired
- Repository may be private and token doesn't have access
- Try: `curl -H "Authorization: Bearer $TOKEN" https://api.github.com/user`

### Vercel API Error: 401 Unauthorized

- Token may be invalid
- Try: `curl -H "Authorization: Bearer $TOKEN" https://api.vercel.com/v1/projects`

### Tests Fail: ModuleNotFoundError

```bash
uv pip install -e ".[dev]" --force-reinstall
```

### Claude CLI Not Found

```bash
curl -fsSL https://claude.ai/install | bash
claude --version  # Should print version
```

## Running Full Test Suite

To run all Wiz tests (mocked, no credentials needed):

```bash
uv run pytest tests/wiz/ -v
```

To run integration tests (requires real credentials):

```bash
# After configuring tokens above:
uv run pytest tests/wiz/test_github_client.py -v
uv run pytest tests/wiz/test_vercel_client.py -v
```

## What Happens in a Feature Pipeline

1. **PLANNED** - Request validated, branch created
2. **IMPLEMENTING** - Code changes applied (manual or Claude)
3. **TESTING** - Test suite runs (must pass)
4. **RISKING** - Final risk assessed from diff
5. **PULL_REQUEST** - Real PR created on GitHub
6. **REVIEWING** - Independent review conducted
7. **MERGING** - All merge gates checked (must all pass)
8. **DEPLOYING** - Wait for Vercel Preview/production
9. **VERIFYING** - Production SHA verified, acceptance tests run
10. **COMPLETE** - Feature verified live in production

## Disabling/Resetting

To disable Wiz (remove credentials):

```bash
rm ~/.config/openjarvis/connectors/github.json
rm ~/.config/openjarvis/connectors/vercel.json
```

To reset test artifacts:

```bash
cd $WIZE_PATH
git checkout main
git branch -D wiz/*  # Delete feature branches
git push origin --delete $(git branch -r | grep 'origin/wiz/')
```

## Next Steps

- [ ] Configure GitHub token
- [ ] Configure Vercel token
- [ ] Verify setup with test commands
- [ ] Run unit tests: `uv run pytest tests/wiz/ -v`
- [ ] Run a manual feature pilot
- [ ] Monitor first autonomous feature in production

## Support

For issues or questions:

1. Check test output: `uv run pytest tests/wiz/ -vv`
2. Verify credentials: check ~/.config/openjarvis/connectors/
3. Review logs: git log to see what Wiz did
4. Check Wiz README: [src/openjarvis/wiz/README.md](src/openjarvis/wiz/README.md)

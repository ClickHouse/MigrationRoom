"""
MigrationRoom — merge `workspace` module outputs into `demo`'s auto-tfvars.

Invoked by `make databricks-provision-workspace` between the two `terraform
apply` calls. Reads the just-applied workspace module's `terraform output
-json` (captured to a file by the Makefile) plus the OAuth credentials from
`workspace/terraform.tfvars`, and writes
sources/databricks/terraform/demo/workspace.auto.tfvars.json — which
Terraform auto-loads, so the chained `demo` apply picks up the new
workspace URL and OAuth credentials with no copy-paste step.

This logic originally lived as a Python heredoc inline in the Makefile
recipe. It was pulled out to its own file because a heredoc spanning
multiple Makefile recipe lines only works under GNU Make's `.ONESHELL`
(added in 3.82); the `make` on this machine is 3.81, which invokes a
separate shell per recipe line by default and silently mis-splits the
heredoc body into standalone (failing) commands. A single-line
`python3 path/to/script.py` call has no such dependency on Make version.

Usage:
    python3 sources/databricks/scripts/merge_workspace_tfvars.py <workspace-output.json>
"""
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKSPACE_DIR = REPO_ROOT / "sources/databricks/terraform/workspace"
DEMO_DIR = REPO_ROOT / "sources/databricks/terraform/demo"


def tfvar(tfvars_text, name):
    m = re.search(rf'^\s*{name}\s*=\s*"([^"]*)"', tfvars_text, re.M)
    return m.group(1) if m else ""


def main():
    if len(sys.argv) != 2:
        print("usage: merge_workspace_tfvars.py <workspace-output.json>", file=sys.stderr)
        return 1

    out = json.loads(Path(sys.argv[1]).read_text())
    tfvars_text = (WORKSPACE_DIR / "terraform.tfvars").read_text()

    payload = {
        "workspace_url": out["workspace_url"]["value"],
        "databricks_client_id": tfvar(tfvars_text, "databricks_client_id"),
        "databricks_client_secret": tfvar(tfvars_text, "databricks_client_secret"),
    }

    dest = DEMO_DIR / "workspace.auto.tfvars.json"
    dest.write_text(json.dumps(payload, indent=2))
    print(f"Wrote {dest.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

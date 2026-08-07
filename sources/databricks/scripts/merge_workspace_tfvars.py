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

# Only matches a single-line, double-quoted value (`name = "value"`). A
# heredoc-style (`<<EOT`) or single-quoted tfvars value for either OAuth
# field will not be found — that hits the same "not present" guard below
# (loud, not silent), so it fails safely rather than writing an empty
# secret, but it's worth knowing this parser doesn't attempt to handle it.
_TFVAR_RE = r'^\s*{name}\s*=\s*"([^"]*)"'


def tfvar(tfvars_text, name):
    """Return the value of `name` from tfvars_text, or None if not found
    as a single-line double-quoted assignment (see module docstring above
    _TFVAR_RE). None, not "", signals "couldn't extract" — the caller
    decides whether that's fatal."""
    m = re.search(_TFVAR_RE.format(name=re.escape(name)), tfvars_text, re.M)
    return m.group(1) if m else None


def main():
    if len(sys.argv) != 2:
        print("usage: merge_workspace_tfvars.py <workspace-output.json>", file=sys.stderr)
        return 1

    out_path = Path(sys.argv[1])
    try:
        out = json.loads(out_path.read_text())
    except FileNotFoundError:
        print(
            f"error: workspace output file not found: {out_path}\n"
            "This is meant to be produced by `terraform output -json` in "
            "sources/databricks/terraform/workspace/ (the Makefile does this "
            "for you via `make databricks-provision-workspace`). Run that "
            "first, or pass the correct path.",
            file=sys.stderr,
        )
        return 1

    tfvars_path = WORKSPACE_DIR / "terraform.tfvars"
    try:
        tfvars_text = tfvars_path.read_text()
    except FileNotFoundError:
        print(
            f"error: {tfvars_path.relative_to(REPO_ROOT)} not found.\n"
            "This merge step reads the OAuth credentials from that file — "
            "it does not see TF_VAR_* environment variables. Run "
            "`cp terraform.tfvars.example terraform.tfvars` in "
            "sources/databricks/terraform/workspace/ and fill in "
            "databricks_client_id / databricks_client_secret there.",
            file=sys.stderr,
        )
        return 1

    client_id = tfvar(tfvars_text, "databricks_client_id")
    client_secret = tfvar(tfvars_text, "databricks_client_secret")

    # Fail loudly rather than writing an empty OAuth credential. The
    # realistic trigger: a partner keeps the secret out of the plaintext
    # tfvars file and supplies it via TF_VAR_databricks_client_secret
    # instead. `workspace/`'s own apply succeeds — Terraform reads env
    # vars natively — but this script reads only the FILE, so without this
    # check it would silently write "" here. demo/'s
    # `!= "" ? x : null` then treats that as "client_id set, secret
    # unset" and the chained apply dies with an opaque OAuth error, far
    # from the actual cause.
    missing = [
        name
        for name, value in (
            ("databricks_client_id", client_id),
            ("databricks_client_secret", client_secret),
        )
        if not value
    ]
    if missing:
        print(
            "error: could not read the following variable(s) from "
            f"{tfvars_path.relative_to(REPO_ROOT)}: {', '.join(missing)}\n"
            "This merge step reads workspace/terraform.tfvars directly — it "
            "does NOT see TF_VAR_* environment variables, even though "
            "`terraform apply` in workspace/ does. If you supplied these via "
            "the environment rather than the file, add them to "
            "workspace/terraform.tfvars as well (or move them there "
            "entirely) so this script can hand them to the demo module.",
            file=sys.stderr,
        )
        return 1

    payload = {
        "workspace_url": out["workspace_url"]["value"],
        "databricks_client_id": client_id,
        "databricks_client_secret": client_secret,
    }

    dest = DEMO_DIR / "workspace.auto.tfvars.json"
    dest.write_text(json.dumps(payload, indent=2))
    print(f"Wrote {dest.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

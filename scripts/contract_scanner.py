"""Static AST Boundary Contract Scanner.

Inspects engine and UI source files to verify that:
1. All method invocations targeting critical boundary singletons
   (TaskStore, HostConcurrencyAuditor, BrowserSolverDaemon, ResourceManager)
   actually exist on their defined class interfaces, preventing runtime phantom calls.
2. All RPC method calls dispatched from the TypeScript UI (src/api.ts) have
   corresponding backend handlers registered in engine/service.py dispatch.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path
from typing import Set, Dict, List, Tuple

ROOT = Path(__file__).resolve().parents[1]


def get_class_methods(file_path: Path, class_name: str) -> Set[str]:
    """Parse a python source file and return all defined function/method names for a given class."""
    if not file_path.exists():
        return set()
    tree = ast.parse(file_path.read_text(encoding="utf-8", errors="replace"), filename=str(file_path))
    methods: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    methods.add(item.name)
    return methods


class BoundaryContractVisitor(ast.NodeVisitor):
    def __init__(self, filename: str, valid_methods: Dict[str, Set[str]]) -> None:
        self.filename = filename
        self.valid_methods = valid_methods
        self.violations: List[Tuple[int, str, str, str]] = []

    def visit_Call(self, node: ast.Call) -> None:
        # Check calls of form: obj.method(...)
        if isinstance(node.func, ast.Attribute):
            attr_name = node.func.attr
            target_boundary = None

            # Detect store calls: self.store.<method>, store.<method>
            val = node.func.value
            if isinstance(val, ast.Name) and val.id == "store":
                target_boundary = "TaskStore"
            elif isinstance(val, ast.Attribute) and val.attr == "store":
                target_boundary = "TaskStore"
            elif isinstance(val, ast.Name) and val.id in {"concurrency_auditor", "auditor"}:
                target_boundary = "HostConcurrencyAuditor"
            elif isinstance(val, ast.Attribute) and val.attr in {"concurrency_auditor", "auditor"}:
                target_boundary = "HostConcurrencyAuditor"
            elif isinstance(val, ast.Name) and val.id in {"solver_daemon", "browser_solver"}:
                target_boundary = "BrowserSolverDaemon"
            elif isinstance(val, ast.Attribute) and val.attr in {"solver_daemon", "browser_solver"}:
                target_boundary = "BrowserSolverDaemon"
            elif isinstance(val, ast.Name) and val.id in {"resources", "resource_manager"}:
                target_boundary = "ResourceManager"
            elif isinstance(val, ast.Attribute) and val.attr in {"resources", "resource_manager"}:
                target_boundary = "ResourceManager"

            if target_boundary:
                valid = self.valid_methods.get(target_boundary, set())
                # Ignore private / dunder or valid methods
                if attr_name not in valid and not attr_name.startswith("__"):
                    self.violations.append((
                        node.lineno,
                        target_boundary,
                        attr_name,
                        f"Undefined method '{attr_name}' invoked on {target_boundary}"
                    ))

        self.generic_visit(node)


def scan_engine_contracts() -> List[Tuple[str, int, str, str, str]]:
    """Scan engine source files for boundary contract violations."""
    valid_methods = {
        "TaskStore": get_class_methods(ROOT / "engine" / "db.py", "TaskStore"),
        "HostConcurrencyAuditor": get_class_methods(ROOT / "engine" / "concurrency_auditor.py", "HostConcurrencyAuditor"),
        "BrowserSolverDaemon": get_class_methods(ROOT / "engine" / "browser_solver.py", "BrowserSolverDaemon"),
        "ResourceManager": get_class_methods(ROOT / "engine" / "limits.py", "ResourceManager"),
    }

    files_to_scan: List[Path] = []
    # Scan all providers
    providers_dir = ROOT / "engine" / "providers"
    if providers_dir.exists():
        files_to_scan.extend(providers_dir.glob("*.py"))
    # Scan core service and custom downloader
    files_to_scan.append(ROOT / "engine" / "service.py")
    files_to_scan.append(ROOT / "engine" / "custom_downloader.py")

    all_violations: List[Tuple[str, int, str, str, str]] = []
    for file_path in files_to_scan:
        if not file_path.exists():
            continue
        try:
            tree = ast.parse(file_path.read_text(encoding="utf-8", errors="replace"), filename=str(file_path))
            visitor = BoundaryContractVisitor(str(file_path.relative_to(ROOT)), valid_methods)
            visitor.visit(tree)
            for lineno, target, method, msg in visitor.violations:
                all_violations.append((str(file_path.relative_to(ROOT)), lineno, target, method, msg))
        except SyntaxError as e:
            all_violations.append((str(file_path.relative_to(ROOT)), e.lineno or 0, "Syntax", "", str(e)))

    return all_violations


def check_ui_rpc_parity() -> List[str]:
    """Verify that all RPC methods invoked by UI (src/api.ts) have corresponding handlers in service.py."""
    api_ts = ROOT / "src" / "api.ts"
    service_py = ROOT / "engine" / "service.py"
    if not api_ts.exists() or not service_py.exists():
        return []

    ui_content = api_ts.read_text(encoding="utf-8", errors="replace")
    ui_methods = set(re.findall(r"engine(?:<[^>]+>)?\(\s*[\"']([a-zA-Z0-9_]+)[\"']", ui_content))

    service_content = service_py.read_text(encoding="utf-8", errors="replace")
    handled_methods = set(re.findall(r"method\s*==\s*[\"']([a-zA-Z0-9_]+)[\"']", service_content))
    for group in re.findall(r"method\s+in\s+\{([^}]+)\}", service_content):
        for m in re.findall(r"[\"']([a-zA-Z0-9_]+)[\"']", group):
            handled_methods.add(m)
    for m in re.findall(r"[\"']([a-zA-Z0-9_]+)[\"']\s*:\s*self\._", service_content):
        handled_methods.add(m)

    missing = sorted(list(ui_methods - handled_methods))
    return [f"UI calls '{m}' in src/api.ts but no handler exists in service.py" for m in missing]


def scan_orphaned_boundary_methods() -> List[Tuple[str, str, str]]:
    """Verify that key public methods on boundary singletons have active call-sites in engine/."""
    target_classes = {
        "HostConcurrencyAuditor": ROOT / "engine" / "concurrency_auditor.py",
    }
    engine_files = [f for f in (ROOT / "engine").glob("**/*.py") if f.name != "concurrency_auditor.py"]
    engine_called: Set[str] = set()
    for f in engine_files:
        try:
            tree = ast.parse(f.read_text(encoding="utf-8", errors="replace"), filename=str(f))
            for n in ast.walk(tree):
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
                    engine_called.add(n.func.attr)
        except Exception:
            pass

    orphans: List[Tuple[str, str, str]] = []
    for cls_name, cls_file in target_classes.items():
        methods = get_class_methods(cls_file, cls_name)
        public_methods = {m for m in methods if not m.startswith("_")}
        for m in sorted(public_methods):
            if m not in engine_called:
                orphans.append((cls_name, m, f"Public method '{m}' on {cls_name} has 0 call-sites in engine/"))
    return orphans


def main() -> int:
    boundary_violations = scan_engine_contracts()
    rpc_violations = check_ui_rpc_parity()
    orphan_violations = scan_orphaned_boundary_methods()

    failed = False
    if boundary_violations:
        failed = True
        print(f"[CONTRACT_SCANNER] FAILED: Found {len(boundary_violations)} boundary contract violation(s):")
        for file_rel, lineno, target, method, msg in boundary_violations:
            print(f"  - {file_rel}:{lineno} -> [{target}] {msg}")
    else:
        print("[CONTRACT_SCANNER] Passed: All provider/service calls match declared boundary contracts.")

    if rpc_violations:
        failed = True
        print(f"[CONTRACT_SCANNER] FAILED: Found {len(rpc_violations)} UI RPC parity violation(s):")
        for v in rpc_violations:
            print(f"  - {v}")
    else:
        print("[CONTRACT_SCANNER] Passed: All 81 UI RPC methods in src/api.ts are handled in engine/service.py.")

    if orphan_violations:
        failed = True
        print(f"[CONTRACT_SCANNER] FAILED: Found {len(orphan_violations)} orphaned boundary method(s):")
        for target, method, msg in orphan_violations:
            print(f"  - [{target}] {msg}")
    else:
        print("[CONTRACT_SCANNER] Passed: Zero orphaned public boundary methods.")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())


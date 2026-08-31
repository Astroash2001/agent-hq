import ast
import asyncio
import sys
import tempfile
import os
from typing import Dict, Any


async def analyze_python_code(code_snippet: str) -> Dict[str, Any]:
    """
    Statically analyze Python code with the AST parser and run it safely to check outputs.
    """
    # 1. AST Syntax Check
    try:
        tree = ast.parse(code_snippet)
        functions = [node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)]
        classes = [node.name for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend([alias.name for alias in node.names])
            elif isinstance(node, ast.ImportFrom):
                imports.append(node.module or "")
    except SyntaxError as e:
        return {
            "success": False,
            "syntax_valid": False,
            "error": f"SyntaxError at line {e.lineno}: {e.msg}",
            "functions": [],
            "classes": [],
            "imports": []
        }

    # 2. Safe execution test in temp file with timeout
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, encoding="utf-8") as temp_file:
        temp_file.write(code_snippet)
        temp_path = temp_file.name

    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            temp_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=8.0)
        os.remove(temp_path)

        return {
            "success": proc.returncode == 0,
            "syntax_valid": True,
            "exit_code": proc.returncode,
            "stdout": stdout.decode("utf-8", errors="replace").strip(),
            "stderr": stderr.decode("utf-8", errors="replace").strip(),
            "functions": functions,
            "classes": classes,
            "imports": imports
        }
    except asyncio.TimeoutError:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        return {
            "success": False,
            "syntax_valid": True,
            "error": "Code execution timed out (8s limit exceeded)."
        }
    except Exception as e:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        return {
            "success": False,
            "syntax_valid": True,
            "error": str(e)
        }

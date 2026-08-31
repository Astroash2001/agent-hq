import os
from typing import Dict, Any, List


async def read_file(filepath: str) -> Dict[str, Any]:
    """Read contents of a text file."""
    try:
        if not os.path.exists(filepath):
            return {"success": False, "error": f"File not found: {filepath}"}

        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()

        return {
            "success": True,
            "filepath": filepath,
            "size_bytes": len(content),
            "content": content[:8000]  # Cap length for LLM context safety
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


async def write_file(filepath: str, content: str) -> Dict[str, Any]:
    """Write or overwrite content to a file."""
    try:
        os.makedirs(os.path.dirname(os.path.abspath(filepath)), exist_ok=True)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)

        return {
            "success": True,
            "filepath": filepath,
            "bytes_written": len(content),
            "message": f"Successfully wrote {len(content)} bytes to {filepath}"
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


async def list_files(directory: str = ".") -> Dict[str, Any]:
    """List files in a directory."""
    try:
        if not os.path.exists(directory):
            return {"success": False, "error": f"Directory not found: {directory}"}

        items = []
        for entry in os.listdir(directory):
            full_path = os.path.join(directory, entry)
            is_dir = os.path.isdir(full_path)
            items.append({
                "name": entry,
                "is_dir": is_dir,
                "size_bytes": os.path.getsize(full_path) if not is_dir else 0
            })

        return {
            "success": True,
            "directory": directory,
            "count": len(items),
            "items": items[:50]
        }
    except Exception as e:
        return {"success": False, "error": str(e)}

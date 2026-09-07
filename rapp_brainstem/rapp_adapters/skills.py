"""Flask skill storage and conversion extension, installed by the OOTB LearnNew agent."""

import hashlib
import hmac
import importlib.util
import json
import os
from pathlib import Path
import re
import tempfile
import threading

from flask import Blueprint, jsonify, request, send_file
from werkzeug.utils import secure_filename
from .skill_frames import emit, skill_receipt


def learner(host):
    state = host.app.extensions["rapp.skills"]
    if not callable(state.get("factory")):
        raise RuntimeError("LearnNew could not load. Restore the bundled learning agent.")
    agent = state["factory"]()
    agent.set_completion_client(host.call_copilot)
    agent.set_output_dir(host.AGENTS_PATH)
    agent.set_skills_dir(getattr(
        host, "SKILLS_PATH", host._resolve_under_base(os.getenv("SKILLS_PATH"), "skills"),
    ))
    return agent


def _discard_bytecode(path):
    try:
        Path(importlib.util.cache_from_source(str(path))).unlink(missing_ok=True)
    except NotImplementedError:
        # Runtimes without a bytecode cache need no invalidation.
        return


def _verify_download(host, payload):
    revision = (request.form.get("source_revision") or "").strip().lower()
    expected = (request.form.get("sha256") or "").strip().lower()
    if revision and revision != host.RAR_REVISION:
        raise ValueError("RAR source revision is not trusted by this brainstem release.")
    if expected:
        if not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise ValueError("Invalid SHA-256 digest.")
        if not hmac.compare_digest(hashlib.sha256(payload).hexdigest(), expected):
            raise ValueError("Skill integrity check failed; downloaded bytes do not match the catalog.")


def _install_generated(host, agent, markdown, filename):
    preview = json.loads(agent.perform(
        action="preview", skill_md=markdown, skill_filename=filename, output_dir=host.AGENTS_PATH,
    ))
    if preview.get("status") != "ok":
        raise RuntimeError(preview.get("message", "LearnNew returned no generated agent."))
    target_name, code = preview.get("filename"), preview.get("code")
    if (not isinstance(target_name, str) or not re.fullmatch(r"[a-z0-9_]+_agent\.py", target_name)
            or not isinstance(code, str) or not code.strip()):
        raise RuntimeError("LearnNew did not return a valid single-file agent.")
    if target_name == "basic_agent.py":
        raise ValueError("basic_agent.py is the shared base class and cannot be replaced.")
    path = Path(host.AGENTS_PATH) / target_name
    marker = code.split("\n", 1)[0].encode("utf-8") + b"\n"
    with host.app.extensions["rapp.skills"]["write_lock"]:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            with path.open("rb") as current:
                if current.readline().replace(b"\r\n", b"\n") != marker:
                    raise FileExistsError(f"{target_name} already exists and is not this skill's generated agent.")
        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".import-", suffix=".py")
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(code.encode("utf-8"))
            loaded = host._load_agent_from_file(temporary)
            if not loaded:
                raise RuntimeError("Generated code could not load; the existing agent was not replaced.")
            for other in path.parent.glob("*_agent.py"):
                if other == path:
                    continue
                if set(loaded).intersection(host._load_agent_from_file(str(other))):
                    raise FileExistsError(f"Generated agent name conflicts with {other.name}.")
            host._atomic_write_bytes(str(path), code.encode("utf-8"))
            _discard_bytecode(path)
        finally:
            Path(temporary).unlink(missing_ok=True)
            _discard_bytecode(temporary)
            with host._quarantine_lock:
                host._quarantined_agents.pop(temporary, None)
    frame = emit(agent.skills_dir, "convert", {
        "filename": target_name, "source_sha256": hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
        "agent_sha256": hashlib.sha256(code.encode("utf-8")).hexdigest(),
    })
    return {"status": "ok", "scope": "persistent", "filename": target_name,
            "agents": list(loaded), "frame": frame, "message": f"Agent {target_name} imported successfully."}


def install(host, factory):
    """Register normal Flask hooks before serving; never alter kernel code or its views."""
    state = host.app.extensions.get("rapp.skills")
    if state is not None:
        state["factory"] = factory
        return
    if host.app._got_first_request:
        raise RuntimeError("Restart Brainstem once to enable the newly installed skills adapter.")
    host.app.extensions["rapp.skills"] = {"factory": factory, "write_lock": threading.RLock()}
    routes = Blueprint("rapp_skills", __name__)

    @routes.route("/skills", methods=["GET"])
    @host._require_secret
    def list_files():
        try:
            agent = learner(host)
            return jsonify({"files": agent.get_skills(), "skills_path": str(agent.skills_dir)})
        except (OSError, RuntimeError) as error:
            return jsonify({"error": str(error)}), 503

    @routes.route("/skills/export/<filename>", methods=["GET"])
    @host._require_secret
    def export_file(filename):
        try:
            path = learner(host).skill_file_path(filename)
            if path.is_symlink():
                raise ValueError("Symbolic-link skills cannot be exported.")
            if not path.is_file():
                return jsonify({"error": "Skill not found"}), 404
            return send_file(path, as_attachment=True)
        except ValueError as error:
            return jsonify({"error": str(error)}), 400
        except (OSError, RuntimeError) as error:
            return jsonify({"error": str(error)}), 503

    @routes.route("/skills/<filename>", methods=["DELETE"])
    @host._require_secret
    def delete_file(filename):
        try:
            agent = learner(host)
            path = agent.skill_file_path(filename)
            if not path.is_file() and not path.is_symlink():
                return jsonify({"error": "Skill not found"}), 404
            path.unlink()
            frame = emit(agent.skills_dir, "delete", {"filename": filename})
            return jsonify({"status": "ok", "message": f"Skill {filename} deleted.", "frame": frame})
        except ValueError as error:
            return jsonify({"error": str(error)}), 400
        except (OSError, RuntimeError) as error:
            return jsonify({"error": str(error)}), 503

    @routes.route("/skills/import", methods=["POST"])
    @host._require_secret
    def import_file():
        if "file" not in request.files or not request.files["file"].filename:
            return jsonify({"error": "No Markdown file uploaded."}), 400
        file = request.files["file"]
        filename = secure_filename(file.filename)
        mode = request.form.get("mode", "skill")
        if Path(filename).suffix.lower() != ".md" or mode not in ("skill", "agent", "remember"):
            return jsonify({"error": "Upload a .md skill with mode=skill or mode=agent."}), 400
        try:
            payload = file.read()
            _verify_download(host, payload)
            markdown = payload.decode("utf-8")
            agent = learner(host)
            if mode == "skill":
                with host.app.extensions["rapp.skills"]["write_lock"]:
                    skill = agent.store_skill(markdown, filename)
                    frame = skill_receipt(agent.skills_dir, "store", skill)
                return jsonify({
                    "status": "ok", "scope": "skill", "filename": skill["filename"],
                    "skill": {key: value for key, value in skill.items() if key != "markdown"},
                    "frame": frame,
                })
            return jsonify(_install_generated(host, agent, markdown, filename))
        except SyntaxError as error:
            return jsonify({"error": f"LearnNew generated invalid Python: {error.msg}"}), 503
        except ValueError as error:
            return jsonify({"error": str(error)}), 400
        except FileExistsError as error:
            return jsonify({"error": str(error)}), 409
        except host.requests.exceptions.RequestException as error:
            return jsonify({"error": f"LearnNew could not reach the model: {error}"}), 502
        except OSError as error:
            return jsonify({"error": str(error)}), 500
        except RuntimeError as error:
            return jsonify({"error": str(error)}), 503

    @host._require_secret
    def invalidate_uploaded_agent_cache():
        file = request.files.get("file")
        if file and file.filename and file.filename.endswith(".py"):
            filename = secure_filename(file.filename)
            if not filename.endswith("_agent.py"):
                filename = filename[:-3] + "_agent.py"
            if filename != "basic_agent.py":
                _discard_bytecode(Path(host.AGENTS_PATH) / filename)

    @host.app.before_request
    def prepare_python_upload():
        if request.path == "/agents/import" and request.method == "POST":
            return invalidate_uploaded_agent_cache()

    host.app.register_blueprint(routes)

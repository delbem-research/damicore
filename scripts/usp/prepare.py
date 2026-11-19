#!/usr/bin/env python3
"""Prepare a local, reviewable USP Inventor Portal dossier from a DAMICORE release."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Iterator
from datetime import date
from pathlib import Path, PurePosixPath

from reportlab import __file__ as reportlab_file
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas

TAG = re.compile(r"v(\d+\.\d+\.\d+)\Z")
SHA512 = re.compile(r"[a-f0-9]{128}\Z")
REVIEW = "[REVISÃO OBRIGATÓRIA:"
SOURCE_FONT = "USP-Vera"
BOLD_FONT = "USP-Vera-Bold"

# The screenshot-derived field inventory is deliberately local to this one tool.
VIABILITY = (
    "application",
    "cost",
    "quality",
    "logistics",
    "performance",
    "production",
    "sustainability",
    "application_requirements",
)
CHOICES = {
    "application": (
        "Um novo produto/processo",
        "Diversos novos produtos/processos",
        "Uma melhoria em um produto/processo existente",
    ),
    "cost": ("Superior", "Inferior", "Similar"),
    "quality": ("Superior", "Inferior", "Similar"),
    "logistics": ("Apresenta Vantagem", "Apresenta Desvantagem", "Equivalente"),
    "performance": ("Superior", "Inferior", "Similar"),
    "production": ("Apresenta Melhoria", "Apresenta Piora", "Não altera"),
    "sustainability": ("Sim", "Não"),
    "application_requirements": (
        "Desenvolvimento de novas máquinas, processos ou insumos",
        "Modificação de novas máquinas, processos ou insumos",
        "Utiliza máquinas, processos ou insumos disponíveis no Brasil",
    ),
}
BOOLEAN_CHOICES = ("Sim", "Não")
TRANSFER_QUESTIONS = (
    "start_company",
    "joint_projects",
    "public_interest",
    "replicable",
    "industrial_investment",
    "tests_performed",
    "legal_requirements",
    "university_company_interaction",
)
UNVERIFIED_DIALOGS = (
    "Incluir Criador",
    "Incluir Instituição",
    "Incluir Produto/Processo Similar",
    "Incluir Anexo",
    "Fomento quando a resposta for Sim",
    "Seletores de tipo, campo de aplicação, derivação e natureza intelectual",
)


class PreparationError(RuntimeError):
    """The requested release, inputs, or safe-output contract is invalid."""


def git(repo: Path, *args: str, binary: bool = False) -> str | bytes:
    try:
        result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True)
    except FileNotFoundError as exc:
        raise PreparationError("git executable not found") from exc
    except subprocess.CalledProcessError as exc:
        message = exc.stderr.decode("utf-8", "replace").strip()
        raise PreparationError(f"git {' '.join(args)} failed: {message}") from exc
    if binary:
        return result.stdout
    return result.stdout.decode("utf-8").strip()


def release(repo: Path, tag: str) -> tuple[str, str]:
    match = TAG.fullmatch(tag)
    if match is None:
        raise PreparationError("release must be an existing stable tag vX.Y.Z")
    try:
        git(repo, "show-ref", "--verify", "--quiet", f"refs/tags/{tag}")
    except PreparationError as exc:
        raise PreparationError(f"release tag {tag} does not exist") from exc
    commit = git(repo, "rev-parse", f"{tag}^{{commit}}")
    assert isinstance(commit, str)
    return match.group(1), commit


def blob(repo: Path, commit: str, path: str) -> bytes:
    raw = git(repo, "show", f"{commit}:{path}", binary=True)
    assert isinstance(raw, bytes)
    return raw


def config_entry(repo: Path, commit: str, package: str) -> tuple[str, dict[str, object]]:
    try:
        configuration = tomllib.loads(blob(repo, commit, "scripts/inpi/config.toml").decode())
    except (UnicodeDecodeError, tomllib.TOMLDecodeError, PreparationError) as exc:
        raise PreparationError(
            "release does not contain a valid INPI software configuration"
        ) from exc
    items = configuration.get("software")
    if not isinstance(items, list):
        raise PreparationError("invalid release INPI software list")
    matches: list[dict[str, object]] = []
    for item in items:
        if not isinstance(item, dict):
            raise PreparationError("invalid release INPI software entry")
        source = item.get("source")
        if not isinstance(source, str):
            raise PreparationError("invalid release source path")
        if (
            source.startswith("/")
            or PurePosixPath(source).as_posix() != source
            or ".." in PurePosixPath(source).parts
            or source == "."
        ):
            raise PreparationError(f"invalid source subtree: {source}")
        if source.replace("_", "-").split("/")[-1] == package:
            matches.append(item)
    if len(matches) != 1:
        raise PreparationError(f"expected exactly one configured software for {package}")
    selected = matches[0]
    source = selected["source"]
    assert isinstance(source, str)
    return source, selected


def source_files(repo: Path, commit: str, source: str) -> tuple[tuple[str, str], ...]:
    prefix = f"{source}/src/"
    listing = git(repo, "ls-tree", "-r", "-z", "--full-tree", commit, "--", prefix, binary=True)
    assert isinstance(listing, bytes)
    files: list[tuple[str, str]] = []
    for record in listing.split(b"\0"):
        if not record:
            continue
        try:
            metadata, raw_path = record.split(b"\t", 1)
            mode, object_type, _sha = metadata.decode("ascii").split()
            path = raw_path.decode("utf-8")
        except (UnicodeDecodeError, ValueError) as exc:
            raise PreparationError("malformed git tree entry") from exc
        if object_type != "blob" or mode not in ("100644", "100755"):
            raise PreparationError(f"unsupported Git entry {path}: {mode} {object_type}")
        if not path.startswith(prefix):
            raise PreparationError(f"source entry escaped subtree: {path}")
        if path.endswith(".py"):
            try:
                content = blob(repo, commit, path).decode("utf-8")
            except UnicodeDecodeError as exc:
                raise PreparationError(f"source file is not UTF-8: {path}") from exc
            files.append((path, content))
    if not files:
        raise PreparationError(f"no Python implementation found in {prefix}")
    return tuple(sorted(files))


def project_metadata(repo: Path, commit: str, source: str, package: str, version: str) -> str:
    try:
        project = tomllib.loads(blob(repo, commit, f"{source}/pyproject.toml").decode())["project"]
        name = project["name"]
        project_version = project["version"]
        description = project["description"]
    except (KeyError, TypeError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise PreparationError(f"invalid pyproject in {source}") from exc
    if name != package or project_version != version or not isinstance(description, str):
        raise PreparationError(f"tagged project metadata does not match {package} {version}")
    return description


def inpi_history(
    repo: Path, version: str, commit: str, source: str, package: str
) -> dict[str, object] | None:
    path = repo / "scripts" / "inpi" / "registrations" / f"{version}.toml"
    if not path.exists():
        return None
    try:
        with path.open("rb") as stream:
            document = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise PreparationError(f"invalid INPI registration record: {path}") from exc
    if document.get("commit") != commit or document.get("release") != f"v{version}":
        raise PreparationError("INPI history does not match the selected Git release")
    entries = document.get("software")
    if not isinstance(entries, list):
        raise PreparationError("invalid INPI registration entries")
    matches = [x for x in entries if isinstance(x, dict) and x.get("project") == package]
    if len(matches) != 1:
        raise PreparationError(f"INPI history has no unique entry for {package}")
    entry = matches[0]
    digest = entry.get("sha512")
    archive = entry.get("archive")
    if (
        entry.get("source") != source
        or not isinstance(digest, str)
        or SHA512.fullmatch(digest) is None
        or archive != f"{package}-{version}-inpi.zip"
    ):
        raise PreparationError("INPI history does not match the registered package")
    assert isinstance(archive, str)
    archive_path = repo / ".temp" / "inpi" / version / archive
    verified = False
    if archive_path.is_file():
        digest_value = hashlib.sha512()
        with archive_path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest_value.update(chunk)
        computed = digest_value.hexdigest()
        if computed != digest:
            raise PreparationError(f"INPI archive checksum mismatch: {archive_path}")
        verified = True
    return {"archive": archive, "sha512": digest, "archive_verified": verified}


def initial_form(package: str, version: str, languages: list[str]) -> dict[str, object]:
    return {
        "program": {
            "title": f"{package.upper()} {version}",
            "protection": "Programa de Computador",
            "creation_date": None,
            "languages": languages,
            "program_types": None,
            "application_fields": None,
            "derivation": None,
            "intellectual_nature": None,
        },
        "creation": {
            "keywords": None,
            "external_institutions": {"participated": None, "records": None},
        },
        "creators": None,
        "technology_transfer": {
            "viability": {
                **{key: {"choice": None, "justification": None} for key in VIABILITY},
                "availability": None,
            },
            "interested_institutions": None,
            "commercialization": {key: None for key in TRANSFER_QUESTIONS},
            "market": {"countries": None, "demand_justification": None},
            "similar_products": None,
            "technology_readiness_level": None,
            "sectors": None,
        },
        "funding": {"received": None, "records": None},
        "attachments": {
            "memorial": "memorial.pdf",
            "justification": "justification.pdf",
            "source_code": "source-code.pdf",
            "additional": None,
        },
    }


def guidance(spec: dict[str, object]) -> dict[str, object]:
    guide: dict[str, object] = {
        "program.creation_date": "Confirm functional completion date, not the release date.",
        "program.program_types": (
            f"Choose USP options. INPI suggestions: {spec.get('program_types')!r}."
        ),
        "program.application_fields": (
            f"Choose USP options. INPI suggestions: {spec.get('application_fields')!r}."
        ),
        "program.derivation": "Confirm adaptation of third-party source, not just methods.",
        "program.intellectual_nature": "Confirm whether another IP category applies.",
        "creation.keywords": "Enter reviewed keywords individually.",
        "creation.external_institutions": "Confirm external partners and dialog records.",
        "creators": "Confirm intellectual contributors, affiliations and shares.",
        "technology_transfer.viability.availability": "Confirm weekly availability.",
        "technology_transfer.interested_institutions": "Confirm actual institutional interest.",
        "technology_transfer.market.countries": "Select countries with justified demand.",
        "technology_transfer.market.demand_justification": "Justify demand in <=500 characters.",
        "technology_transfer.similar_products": "Name comparable products and differences.",
        "technology_transfer.technology_readiness_level": "Confirm TRL 1-9 with evidence.",
        "technology_transfer.sectors": "Choose the exact USP sector options.",
        "funding.received": "Confirm grants, scholarships and associated support.",
        "funding.records": "If funded, enter each relevant funding record.",
        "attachments.additional": "Check any additional AUSPIN requirements.",
        "unverified_dialogs": list(UNVERIFIED_DIALOGS),
    }
    for key in VIABILITY:
        guide[f"technology_transfer.viability.{key}"] = (
            "Compare with evidence and justify in <=250 characters."
        )
    for key in TRANSFER_QUESTIONS:
        guide[f"technology_transfer.commercialization.{key}"] = (
            "Confirm this declaration with the relevant creator."
        )
    return guide


def memorial_template(
    title: str, version: str, description: str, source: str, snippet: tuple[str, str]
) -> str:
    snippet_path, snippet_text = snippet
    lines = snippet_text.splitlines()[:22]
    return (
        f"# Memorial Descritivo - {title} {version}\n\n"
        "## Introdução\n\n"
        f"Descrição do pacote na release (revisar e traduzir): {description.strip()}\n\n"
        "[REVISÃO OBRIGATÓRIA: Descrever contexto, finalidade e criadores efetivos.]\n\n"
        "## Desenvolvimento\n\n"
        f"Implementação em Python, com arquivos-fonte em `{source}/src/` na versão indicada.\n\n"
        "[REVISÃO OBRIGATÓRIA: Descrever módulos, entradas, saídas e contribuição técnica.]\n\n"
        f"Trecho representativo do código-fonte (`{snippet_path}`):\n\n"
        "```python\n" + "\n".join(lines) + "\n```\n\n"
        "## Conclusão\n\n"
        "[REVISÃO OBRIGATÓRIA: Resumir aplicações verificáveis e delimitar o software.]\n"
    )


def justification_template(title: str, version: str, description: str) -> str:
    return (
        f"# Justificativa do Pedido de Registro - {title} {version}\n\n"
        "## Aplicações\n\n"
        f"Descrição do pacote na release (revisar e traduzir): {description.strip()}\n\n"
        "[REVISÃO OBRIGATÓRIA: Confirmar aplicações e público-alvo.]\n\n"
        "## Soluções comparáveis\n\n"
        "[REVISÃO OBRIGATÓRIA: Identificar similares e diferenças comprovadas.]\n\n"
        "## Potencial de utilização ou comercialização\n\n"
        "[REVISÃO OBRIGATÓRIA: Justificar o potencial sem inventar receitas.]\n\n"
        "## Fundamentação do pedido\n\n"
        "[REVISÃO OBRIGATÓRIA: Fundamentar o registro desta implementação.]\n"
    )


def register_fonts() -> None:
    fonts = Path(reportlab_file).parent / "fonts"
    if SOURCE_FONT not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(SOURCE_FONT, str(fonts / "Vera.ttf")))
        pdfmetrics.registerFont(TTFont(BOLD_FONT, str(fonts / "VeraBd.ttf")))


def text_width(line: str, font: str, size: float) -> float:
    return float(pdfmetrics.stringWidth(line, font, size))


def wrap(line: str, font: str, size: float, width: float) -> Iterator[str]:
    if not line:
        yield ""
        return
    while line:
        end = len(line)
        while end > 1 and text_width(line[:end], font, size) > width:
            end -= 1
        if end == 1 and text_width(line[:end], font, size) > width:
            raise PreparationError("a character cannot fit on the PDF page")
        yield line[:end]
        line = line[end:]


def guard_unicode(value: str, path: str, line: int) -> None:
    font = pdfmetrics.getFont(SOURCE_FONT)
    mapping = font.face.charToGlyph
    for char in value:
        if ord(char) not in mapping:
            raise PreparationError(f"unrenderable Unicode U+{ord(char):04X} at {path}:{line}")


def pdf_source(
    path: Path, title: str, version: str, commit: str, files: tuple[tuple[str, str], ...]
) -> None:
    register_fonts()
    canvas = Canvas(str(path), pagesize=A4, pageCompression=1)
    canvas.setTitle(f"{title} {version} - source code")
    canvas.setAuthor("DAMICORE")
    page = 0
    y = 0.0
    left = 40.0
    content_width = float(A4[0]) - 2 * left - 37

    def next_page() -> None:
        nonlocal page, y
        if page:
            canvas.setFont(SOURCE_FONT, 8)
            canvas.drawRightString(float(A4[0]) - left, 25, f"Page {page}")
            canvas.showPage()
        page += 1
        y = float(A4[1]) - 44
        canvas.setFont(BOLD_FONT, 10)
        canvas.drawString(left, y, f"{title} {version}")
        y -= 16
        canvas.setFont(SOURCE_FONT, 7)
        canvas.drawString(left, y, f"Git commit: {commit}")
        y -= 23

    next_page()
    for file_path, source in files:
        guard_unicode(file_path, file_path, 0)
        if y < 90:
            next_page()
        canvas.setFont(BOLD_FONT, 9)
        for piece in wrap(file_path, BOLD_FONT, 9, content_width + 24):
            if y < 70:
                next_page()
            canvas.drawString(left, y, piece)
            y -= 13
        y -= 6
        for number, original in enumerate(source.splitlines(), start=1):
            line = original.expandtabs(4)
            guard_unicode(line, file_path, number)
            first = True
            for piece in wrap(line, SOURCE_FONT, 7.1, content_width):
                if y < 47:
                    next_page()
                canvas.setFont(SOURCE_FONT, 6.7)
                canvas.drawRightString(left + 27, y, str(number) if first else "+")
                canvas.setFont(SOURCE_FONT, 7.1)
                canvas.drawString(left + 36, y, piece)
                y -= 10.2
                first = False
        y -= 17
    canvas.setFont(SOURCE_FONT, 8)
    canvas.drawRightString(float(A4[0]) - left, 25, f"Page {page}")
    canvas.save()


def pdf_markdown(path: Path, text: str, title: str) -> None:
    register_fonts()
    canvas = Canvas(str(path), pagesize=A4)
    canvas.setTitle(title)
    page = 0
    y = 0.0
    left = 52.0
    width = float(A4[0]) - 2 * left
    code = False

    def page_start() -> None:
        nonlocal page, y
        if page:
            canvas.setFont(SOURCE_FONT, 8)
            canvas.drawRightString(float(A4[0]) - left, 28, str(page))
            canvas.showPage()
        page += 1
        y = float(A4[1]) - 56

    page_start()
    for number, raw in enumerate(text.splitlines(), start=1):
        if raw.startswith("```"):
            code = not code
            y -= 5
            continue
        if raw.startswith("# "):
            font, size, leading, line = BOLD_FONT, 14.0, 19.0, raw[2:]
        elif raw.startswith("## "):
            font, size, leading, line = BOLD_FONT, 11.2, 16.0, raw[3:]
        elif code:
            font, size, leading, line = SOURCE_FONT, 8.0, 11.0, raw.expandtabs(4)
        else:
            font, size, leading, line = SOURCE_FONT, 9.5, 14.5, raw
        guard_unicode(line, title, number)
        if not line:
            y -= 10
            continue
        for piece in wrap(line, font, size, width):
            if y < 54:
                page_start()
            canvas.setFont(font, size)
            canvas.drawString(left, y, piece)
            y -= leading
    canvas.setFont(SOURCE_FONT, 8)
    canvas.drawRightString(float(A4[0]) - left, 28, str(page))
    canvas.save()


def safe_output(repo: Path, output: Path) -> None:
    try:
        relative = output.relative_to(repo)
    except ValueError:
        return
    try:
        git(repo, "check-ignore", "--quiet", "--no-index", "--", relative.as_posix())
    except PreparationError as exc:
        raise PreparationError("output inside repository must be Git-ignored") from exc


def prepare(repo: Path, tag: str, package: str, case: str | None, output: Path) -> None:
    version, commit = release(repo, tag)
    source, spec = config_entry(repo, commit, package)
    description = project_metadata(repo, commit, source, package, version)
    files = source_files(repo, commit, source)
    history = inpi_history(repo, version, commit, source, package)
    safe_output(repo, output)
    if output.exists():
        raise PreparationError(f"output directory already exists: {output}")
    if case is not None and re.fullmatch(r"PC-\d{4}-\d+", case) is None:
        raise PreparationError("USP case should have the form PC-YYYY-NNNN")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="usp-stage-", dir=output.parent) as temporary:
        staging = Path(temporary)
        languages = spec.get("languages")
        if not isinstance(languages, list) or not all(isinstance(x, str) for x in languages):
            raise PreparationError("invalid registered languages")
        title = spec.get("title")
        if not isinstance(title, str):
            raise PreparationError("invalid registered title")
        dossier = {
            "schema_version": 1,
            "identity": {
                "package": package,
                "version": version,
                "tag": tag,
                "commit": commit,
                "source": source,
                "usp_case": case,
                "inpi": history,
            },
            "form": initial_form(package, version, languages),
            "guidance": guidance(spec),
        }
        (staging / "form.json").write_text(
            json.dumps(dossier, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        (staging / "memorial.md").write_text(
            memorial_template(title, version, description, source, files[0]), encoding="utf-8"
        )
        (staging / "justification.md").write_text(
            justification_template(title, version, description), encoding="utf-8"
        )
        pdf_source(staging / "source-code.pdf", title, version, commit, files)
        if output.exists():
            raise PreparationError(f"output directory appeared during preparation: {output}")
        os.rename(staging, output)


def field_errors(form: dict[str, object]) -> list[str]:
    errors: list[str] = []
    program = form.get("program")
    if not isinstance(program, dict):
        return ["form.program must be an object"]
    value = program.get("creation_date")
    if value is not None:
        try:
            if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
                raise ValueError("invalid date")
        except ValueError:
            errors.append("program.creation_date must be a real YYYY-MM-DD date")
    for field in ("title", "protection"):
        if not isinstance(program.get(field), str) or not program[field]:
            errors.append(f"program.{field} must be non-empty")
    languages = program.get("languages")
    if (
        not isinstance(languages, list)
        or not languages
        or any(not isinstance(language, str) or not language.strip() for language in languages)
    ):
        errors.append("program.languages must contain non-empty names")

    creation = form.get("creation")
    if not isinstance(creation, dict):
        return errors + ["form.creation must be an object"]
    external = creation.get("external_institutions")
    if not isinstance(external, dict):
        errors.append("creation.external_institutions must be an object")
    else:
        participated = external.get("participated")
        records = external.get("records")
        if participated is not None and participated not in BOOLEAN_CHOICES:
            errors.append("external_institutions.participated must be Sim or Não")
        if participated == "Sim" and (not isinstance(records, list) or not records):
            errors.append("external_institutions.records required when participated is Sim")
        if participated == "Não" and records not in (None, []):
            errors.append("external_institutions.records must be empty when participated is Não")

    creators = form.get("creators")
    if creators is not None and (not isinstance(creators, list) or not creators):
        errors.append("creators must be a non-empty list when provided")

    transfer = form.get("technology_transfer")
    if not isinstance(transfer, dict):
        return errors + ["form.technology_transfer must be an object"]
    viability = transfer.get("viability")
    if not isinstance(viability, dict):
        return errors + ["form.technology_transfer.viability must be an object"]
    for key in VIABILITY:
        item = viability.get(key)
        if not isinstance(item, dict):
            errors.append(f"viability.{key} must be an object")
            continue
        choice, reason = item.get("choice"), item.get("justification")
        if choice is not None and (not isinstance(choice, str) or choice not in CHOICES[key]):
            errors.append(f"viability.{key}.choice must match a visible portal option")
        if reason is not None and (not isinstance(reason, str) or len(reason) > 250):
            errors.append(f"viability.{key}.justification exceeds 250 characters or is not text")
    market = transfer.get("market")
    if not isinstance(market, dict):
        errors.append("technology_transfer.market must be an object")
    else:
        reason = market.get("demand_justification")
        if reason is not None and (not isinstance(reason, str) or len(reason) > 500):
            errors.append("market.demand_justification exceeds 500 characters or is not text")
    trl = transfer.get("technology_readiness_level")
    if trl is not None and (not isinstance(trl, int) or isinstance(trl, bool) or not 1 <= trl <= 9):
        errors.append("technology_readiness_level must be an integer 1-9")
    funding = form.get("funding")
    if not isinstance(funding, dict):
        errors.append("funding must be an object")
    else:
        received, records = funding.get("received"), funding.get("records")
        if received is not None and received not in BOOLEAN_CHOICES:
            errors.append("funding.received must be Sim or Não")
        if received == "Sim" and (not isinstance(records, list) or not records):
            errors.append("funding.records required when funding is Sim")
        if received == "Não" and records not in (None, []):
            errors.append("funding.records must be empty when funding is Não")
    return errors


def pending_fields(node: object, prefix: str = "form") -> list[str]:
    if isinstance(node, dict):
        found: list[str] = []
        for key, value in node.items():
            found.extend(pending_fields(value, f"{prefix}.{key}"))
        return found
    return [prefix] if node is None else []


def finalize(output: Path) -> tuple[int, int]:
    if not output.is_dir():
        raise PreparationError(f"not a USP preparation directory: {output}")
    try:
        dossier = json.loads((output / "form.json").read_text(encoding="utf-8"))
        if not isinstance(dossier, dict) or dossier.get("schema_version") != 1:
            raise PreparationError("invalid USP form schema version")
        form = dossier["form"]
        if not isinstance(form, dict):
            raise PreparationError("form must be an object")
        memorial = (output / "memorial.md").read_text(encoding="utf-8")
        justification = (output / "justification.md").read_text(encoding="utf-8")
    except (OSError, ValueError, KeyError) as exc:
        raise PreparationError(f"incomplete or invalid USP dossier: {exc}") from exc
    errors = field_errors(form)
    if errors:
        raise PreparationError("invalid form fields: " + "; ".join(errors))
    drafts = {"memorial.pdf": memorial, "justification.pdf": justification}
    for name, content in drafts.items():
        if REVIEW in content or not content.strip():
            raise PreparationError(f"{name} contains an unreviewed placeholder")
    if not (output / "source-code.pdf").is_file():
        raise PreparationError("missing source-code.pdf")
    with tempfile.TemporaryDirectory(prefix="usp-pdf-", dir=output) as temporary:
        temp = Path(temporary)
        for name, content in drafts.items():
            pdf_markdown(temp / name, content, name)
        for name in drafts:
            os.replace(temp / name, output / name)
    pending = pending_fields(form)
    return len(pending), len(UNVERIFIED_DIALOGS)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("release", nargs="?", help="stable release tag (vX.Y.Z)")
    parser.add_argument("--package", help="one of the packages in the tagged INPI configuration")
    parser.add_argument("--case", help="optional USP communication number")
    parser.add_argument("--out", type=Path, help="initial output directory")
    parser.add_argument(
        "--finalize", type=Path, help="validate an edited dossier and render two PDFs"
    )
    args = parser.parse_args(argv)
    if args.finalize is not None:
        if args.release or args.package or args.case or args.out:
            parser.error("--finalize cannot be combined with preparation arguments")
    elif not args.release or not args.package:
        parser.error("release and --package are required")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.finalize is not None:
            pending, dialogs = finalize(args.finalize.resolve())
            print(f"USP PDFs generated. Pending: {pending}; unverified dialogs: {dialogs}.")
            print("Review remaining requirements in the actual USP portal before submission.")
        else:
            assert isinstance(args.release, str) and isinstance(args.package, str)
            root = git(Path(__file__).resolve().parent, "rev-parse", "--show-toplevel")
            assert isinstance(root, str)
            repo = Path(root).resolve()
            version_match = TAG.fullmatch(args.release)
            if version_match is None:
                raise PreparationError("release must be vX.Y.Z")
            version = version_match.group(1)
            output = (args.out or repo / ".temp" / "usp" / version / args.package).resolve()
            prepare(repo, args.release, args.package, args.case, output)
            print(f"USP dossier prepared: {output}")
            print("Review form.json, memorial.md and justification.md, then run --finalize.")
    except PreparationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())

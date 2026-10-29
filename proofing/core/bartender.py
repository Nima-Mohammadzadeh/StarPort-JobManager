from __future__ import annotations

import html
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional


class BarTenderError(RuntimeError):
    pass


@dataclass
class BarTenderDiagnostics:
    executable: str = ""
    executable_found: bool = False
    com_available: bool = False
    version: str = ""
    edition: str = ""
    detail: str = ""


FIELD_ALIASES = {
    "customer": ["CUSTOMER", "CUSTOMER_NAME", "CLIENT", "CLIENT_NAME", "CUST"],
    "job_number": ["JOB_NUMBER", "JOBNO", "JOB_NUM", "JOB", "WORK_ORDER", "WO"],
    "part_number": ["PART_NUMBER", "PARTNO", "PART_NUM", "PART", "SKU", "ITEM_NUMBER"],
    "gtin": ["GTIN", "GTIN14", "UPC", "UPC_A", "UPC12", "BARCODE_DATA"],
    "barcode_type_1": ["BARCODE_TYPE_1", "BARCODE1_TYPE", "PRIMARY_BARCODE_TYPE", "BARCODE_TYPE"],
    "barcode_data_1": ["BARCODE_DATA_1", "BARCODE1_DATA", "PRIMARY_BARCODE_DATA", "BARCODE_DATA", "UPC_DATA"],
    "barcode_type_2": ["BARCODE_TYPE_2", "BARCODE2_TYPE", "SECONDARY_BARCODE_TYPE"],
    "barcode_data_2": ["BARCODE_DATA_2", "BARCODE2_DATA", "SECONDARY_BARCODE_DATA"],
    "font_size_callout": ["FONT_SIZE", "FONT_SIZE_PT", "TEXT_SIZE", "PRIMARY_FONT_SIZE"],
    "epc": ["EPC", "EPC_HEX", "RFID_EPC", "EPC96"],
    "serial": ["SERIAL", "SERIAL_NUMBER", "SERIALNO", "EPC_SERIAL"],
    "quantity": ["QUANTITY", "QTY", "LABEL_QTY"],
    "revision": ["REVISION", "REV", "LABEL_REVISION"],
    "proof_revision": ["PROOF_REVISION", "PROOF_REV", "PROOFREV"],
    "inlay": ["INLAY", "RFID_INLAY", "INLAY_NAME"],
    "chip": ["CHIP", "RFID_CHIP", "IC", "IC_TYPE"],
}


def _norm(value: str) -> str:
    return "".join(ch for ch in str(value).upper() if ch.isalnum())


def suggest_mapping(discovered_names: List[str], current: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    current = dict(current or {})
    norm_lookup = {_norm(name): name for name in discovered_names if name}
    result: Dict[str, str] = {}
    for key, aliases in FIELD_ALIASES.items():
        existing = current.get(key, "")
        if existing and _norm(existing) in norm_lookup:
            result[key] = norm_lookup[_norm(existing)]
            continue
        match = ""
        for alias in aliases:
            if _norm(alias) in norm_lookup:
                match = norm_lookup[_norm(alias)]
                break
        if not match:
            # Conservative contains-match only when it is unique.
            candidates = [name for name in discovered_names if any(_norm(alias) in _norm(name) for alias in aliases)]
            if len(candidates) == 1:
                match = candidates[0]
        result[key] = match or existing
    for key, value in current.items():
        result.setdefault(key, value)
    return result


class BarTenderPreviewProvider:
    """Render BarTender previews with BTXML and optionally inspect templates using COM."""

    def __init__(self, executable: str = "", timeout_seconds: int = 90):
        self.executable = executable or self.detect_executable() or ""
        self.timeout_seconds = timeout_seconds

    @staticmethod
    def detect_executable() -> Optional[str]:
        candidates: List[Path] = []
        which = shutil.which("bartend.exe") or shutil.which("bartend")
        if which:
            candidates.append(Path(which))
        for env_name in ("ProgramFiles", "ProgramFiles(x86)"):
            base = os.environ.get(env_name)
            if not base:
                continue
            base_path = Path(base)
            candidates.extend([
                base_path / "Seagull" / "BarTender Suite" / "bartend.exe",
                base_path / "Seagull Scientific" / "BarTender" / "bartend.exe",
                base_path / "Seagull Scientific" / "BarTender Suite" / "bartend.exe",
            ])
            # Current installs can include versioned BarTender folders.
            for parent in (base_path / "Seagull Scientific", base_path / "Seagull"):
                if parent.exists():
                    candidates.extend(parent.glob("**/bartend.exe"))
        seen = set()
        for candidate in candidates:
            try:
                resolved = str(candidate.resolve())
            except OSError:
                resolved = str(candidate)
            if resolved.lower() in seen:
                continue
            seen.add(resolved.lower())
            if candidate.exists():
                return str(candidate)
        return None

    @property
    def available(self) -> bool:
        return bool(self.executable and Path(self.executable).exists())

    def diagnose(self) -> BarTenderDiagnostics:
        diag = BarTenderDiagnostics(
            executable=self.executable,
            executable_found=self.available,
            detail="BarTender executable detected." if self.available else "BarTender executable not found.",
        )
        if os.name != "nt":
            diag.detail += " COM inspection is available only on Windows."
            return diag
        try:
            import win32com.client  # type: ignore
        except Exception as exc:
            diag.detail += f" pywin32 COM support unavailable: {exc}"
            return diag

        app = None
        try:
            app = win32com.client.Dispatch("BarTender.Application")
            app.Visible = False
            diag.com_available = True
            try:
                diag.version = str(app.Version)
            except Exception:
                pass
            try:
                diag.edition = str(app.Edition)
            except Exception:
                pass
            diag.detail += " Automation COM interface responded successfully."
        except Exception as exc:
            diag.detail += f" Automation COM connection failed: {exc}"
        finally:
            if app is not None:
                try:
                    app.Quit(1)  # btDoNotSaveChanges
                except Exception:
                    pass
        return diag

    def inspect_template_named_sources(self, template_path: str) -> List[str]:
        path = Path(template_path)
        if not path.exists() or path.suffix.lower() != ".btw":
            raise BarTenderError("Select an existing .BTW template before inspecting data sources.")
        if os.name != "nt":
            raise BarTenderError("BarTender template inspection is available only on Windows.")
        try:
            import win32com.client  # type: ignore
        except Exception as exc:
            raise BarTenderError("pywin32 is required for template inspection. Run Install_Dependencies.bat.") from exc

        app = None
        try:
            app = win32com.client.Dispatch("BarTender.Application")
            app.Visible = False
            fmt = app.Formats.Open(str(path.resolve()), False, "")

            # BETA21_5_PREVIEW_DATABASE_BYPASS_V3
            # Preview-only safety: do not use the production database.
            try:
                fmt.UseDatabase = False
            except Exception:
                pass

            # Some preview methods honor PrintSetup.UseDatabase directly.
            try:
                fmt.PrintSetup.UseDatabase = False
            except Exception:
                pass

            # Avoid BarTender attempting live record navigation for preview.
            try:
                fmt.ViewRecordNavigator.Enabled = False
            except Exception:
                pass

            names: List[str] = []
            try:
                raw = str(fmt.UsedNames(0, "\n") or "")  # btUsedNamedSubStrings = 0
                names = [item.strip() for item in raw.replace("\r", "\n").split("\n") if item.strip()]
            except Exception:
                raw = str(fmt.NamedSubStrings.GetAll("=", "\n") or "")
                for record in raw.replace("\r", "\n").split("\n"):
                    record = record.strip()
                    if record:
                        names.append(record.split("=", 1)[0].strip())
            return sorted(set(names), key=str.casefold)
        except Exception as exc:
            raise BarTenderError(f"Unable to inspect BarTender template: {exc}") from exc
        finally:
            if app is not None:
                try:
                    app.Quit(1)  # btDoNotSaveChanges
                except Exception:
                    pass

    # BETA21_5_BTXMl_PREVIEW_DATABASE_BYPASS_FINAL
    def build_btxml(self, template_path: str, output_dir: str, named_data: Dict[str, str], dpi: int = 300) -> str:
        template = html.escape(str(Path(template_path).resolve()))
        folder = html.escape(str(Path(output_dir).resolve()))
        named = []
        for name, value in named_data.items():
            if not name:
                continue
            named.append(
                f'<NamedSubString Name="{html.escape(str(name), quote=True)}">'
                f'<Value>{html.escape(str(value))}</Value>'
                f'</NamedSubString>'
            )
        named_xml = "\n".join(named)
        return f'''<?xml version="1.0" encoding="utf-8"?>
<XMLScript Version="2.0" Name="RFIDProofPreview" ID="RFIDProofPreview">
  <Command Name="Preview">
    <ExportPrintPreviewToImage ReturnImageInResponse="false">
      <Format>{template}</Format>
      <PrintSetup>
        <UseDatabase>false</UseDatabase>
        <RecordRange>1</RecordRange>
      </PrintSetup>
      <Folder>{folder}</Folder>
      <FileNameTemplate>preview_%PageNumber%.png</FileNameTemplate>
      <ImageFormatType>PNG</ImageFormatType>
      <Colors>btColors24Bit</Colors>
      <DPI>{int(dpi)}</DPI>
      <Overwrite>true</Overwrite>
      <IncludeMargins>true</IncludeMargins>
      <IncludeBorder>true</IncludeBorder>
      <BackgroundColor>16777215</BackgroundColor>
      {named_xml}
    </ExportPrintPreviewToImage>
  </Command>
</XMLScript>
'''

    def render_preview(self, template_path: str, output_dir: str, named_data: Dict[str, str], dpi: int = 300) -> str:
        if not Path(template_path).exists():
            raise BarTenderError(f"Template not found: {template_path}")
        if not self.available:
            raise BarTenderError(
                "BarTender executable was not found. Open Tools > Settings to select bartend.exe, "
                "or use Select Preview Image for fallback proofing."
            )

        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        for old in out.glob("preview_*.png"):
            try:
                old.unlink()
            except OSError:
                pass

        script_path = out / "rfid_proof_preview.btxml"
        script_path.write_text(self.build_btxml(template_path, str(out), named_data, dpi), encoding="utf-8")

        cmd = [self.executable, "/RUN", "/MIN=SystemTray", "/NOSPLASH", "/X", f"/XMLScript={script_path}"]
        try:
            completed = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout_seconds, check=False)
        except subprocess.TimeoutExpired as exc:
            raise BarTenderError("BarTender preview timed out.") from exc

        deadline = time.time() + 10
        preview = None
        while time.time() < deadline:
            files = list(out.glob("preview_*.png"))
            if files:
                # BarTender can export multiple preview pages. Always use page 1 so
                # customer proofs are deterministic and never pick the last-created page.
                def _page_number(path: Path) -> int:
                    match = re.search(r"preview_(\d+)\.png$", path.name, re.IGNORECASE)
                    return int(match.group(1)) if match else 10**9

                files.sort(key=lambda p: (_page_number(p), p.name.casefold()))
                preview = files[0]
                break
            time.sleep(0.25)

        if not preview:
            details = (completed.stderr or completed.stdout or "").strip()
            raise BarTenderError(
                "BarTender did not create a preview image. Check the .BTW file, named data-source "
                "mapping, printer assignment, and BarTender automation licensing."
                + (f"\n\nBarTender output:\n{details}" if details else "")
            )
        return str(preview)

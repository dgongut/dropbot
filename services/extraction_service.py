"""
Servicio de descompresión de archivos (ZIP, TAR, RAR).
"""
import os
import shutil
import zipfile
import tarfile
import rarfile

from debug import debug, warning, error


def extract_file(file_path, extract_to):
    try:
        filename = os.path.basename(file_path)
        if file_path.lower().endswith('.zip'):
            debug(f"[EXTRACT] Extracting ZIP file: {filename}")
            with zipfile.ZipFile(file_path, 'r') as zip_ref:
                file_count = len(zip_ref.namelist())
                debug(f"[EXTRACT] ZIP contains {file_count} files")
                zip_ref.extractall(extract_to)
            debug(f"[EXTRACT] ZIP extraction completed: {filename}")
        elif any(file_path.lower().endswith(ext) for ext in ['.tar', '.tar.gz', '.tgz', '.tar.bz2', '.tbz']):
            debug(f"[EXTRACT] Extracting TAR file: {filename}")
            with tarfile.open(file_path, 'r:*') as tar_ref:
                file_count = len(tar_ref.getmembers())
                debug(f"[EXTRACT] TAR contains {file_count} files")
                tar_ref.extractall(extract_to)
            debug(f"[EXTRACT] TAR extraction completed: {filename}")
        elif rarfile.is_rarfile(file_path):
            try:
                debug(f"[EXTRACT] Extracting RAR file: {filename}")
                with rarfile.RarFile(file_path) as rar_ref:
                    file_count = len(rar_ref.namelist())
                    debug(f"[EXTRACT] RAR contains {file_count} files")
                    rar_ref.extractall(extract_to)
                debug(f"[EXTRACT] RAR extraction completed: {filename}")
            except rarfile.RarCannotExec as e:
                error(f"[EXTRACT] RAR extraction tool not found: {e}")
                return False
            except Exception as e:
                msg = str(e).lower()
                if ("need to start from first volume" in msg or
                    "need first volume" in msg or
                    "missing volume" in msg or
                    "unexpected end of archive" in msg):
                    warning(f"[EXTRACT] File {file_path} - Missing RAR parts")
                    # Eliminar carpeta vacía creada
                    if os.path.exists(extract_to):
                        try:
                            shutil.rmtree(extract_to)
                            debug(f"[EXTRACT] Deleted empty folder after missing parts error: {extract_to}")
                        except Exception as cleanup_error:
                            warning(f"[EXTRACT] Error deleting empty folder {extract_to}: {cleanup_error}")
                    return "missing_parts"
                elif ("failed the read enough data" in msg):
                    # Este error puede ocurrir en archivos RAR válidos cuando 7z intenta leer más datos
                    # de los disponibles al final del archivo. Verificamos si la extracción fue completa.
                    if os.path.exists(extract_to) and os.listdir(extract_to):
                        # Verificar si hay archivos de 0 bytes (señal de extracción incompleta)
                        has_zero_byte_files = False
                        zero_byte_count = 0
                        total_files = 0

                        for root, dirs, files in os.walk(extract_to):
                            for filename in files:
                                total_files += 1
                                file_full_path = os.path.join(root, filename)
                                if os.path.getsize(file_full_path) == 0:
                                    has_zero_byte_files = True
                                    zero_byte_count += 1
                                    debug(f"[EXTRACT] Found zero-byte file: {file_full_path}")

                        if has_zero_byte_files:
                            warning(f"[EXTRACT] File {file_path} - Partial extraction: {zero_byte_count}/{total_files} files are empty or incomplete")
                            # Borrar la carpeta parcialmente extraída
                            try:
                                shutil.rmtree(extract_to)
                                debug(f"[EXTRACT] Deleted partially extracted folder: {extract_to}")
                            except Exception as cleanup_error:
                                warning(f"[EXTRACT] Error deleting partially extracted folder {extract_to}: {cleanup_error}")
                            return "partial"
                        else:
                            debug(f"[EXTRACT] File {file_path} - Extraction completed despite read warning")
                            return True
                    else:
                        error(f"[EXTRACT] File {file_path} - Corrupted or incomplete RAR file: {e}")
                        # Eliminar carpeta vacía creada
                        if os.path.exists(extract_to):
                            try:
                                shutil.rmtree(extract_to)
                                debug(f"[EXTRACT] Deleted empty folder after corruption error: {extract_to}")
                            except Exception as cleanup_error:
                                warning(f"[EXTRACT] Error deleting empty folder {extract_to}: {cleanup_error}")
                        return "corrupted"
                elif ("corrupt" in msg or
                      "damaged" in msg or
                      "bad rar file" in msg or
                      "crc failed" in msg or
                      "checksum error" in msg):
                    error(f"[EXTRACT] File {file_path} - Corrupted or incomplete RAR file: {e}")
                    # Eliminar carpeta vacía creada
                    if os.path.exists(extract_to):
                        try:
                            shutil.rmtree(extract_to)
                            debug(f"[EXTRACT] Deleted empty folder after corruption error: {extract_to}")
                        except Exception as cleanup_error:
                            warning(f"[EXTRACT] Error deleting empty folder {extract_to}: {cleanup_error}")
                    return "corrupted"
                else:
                    raise
        else:
            return False
        return True

    except Exception as e:
        error(f"[EXTRACT] Error extracting file {file_path}: {e}")
        if os.path.exists(extract_to):
            try:
                shutil.rmtree(extract_to)
                debug(f"[EXTRACT] Deleted folder: {extract_to}")
            except Exception as cleanup_error:
                warning(f"[EXTRACT] Error deleting folder {extract_to}: {cleanup_error}")
        return False

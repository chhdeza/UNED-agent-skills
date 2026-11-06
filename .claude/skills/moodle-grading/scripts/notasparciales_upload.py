"""
notasparciales_upload.py
========================

Sube notas al sistema oficial de UNED Notas Parciales:

    https://produccion.uned.ac.cr/notasparciales/Formularios/CapturaNotas.aspx

Es un sistema ASP.NET WebForms con PageMethods JSON. NO es Moodle. La página
es un SPA-light: una sola URL y todas las acciones (cargar tabla, guardar,
recargar fila) son POST a `CapturaNotas.aspx/<webMethod>` con bodies JSON.

Autenticación
-------------
Para evitar tener que reproducir el SSO institucional, este script usa el
patrón "bring-your-own-cookies": vos te logueás normalmente en el navegador,
copiás las 3 cookies de sesión a `.env`, y el script las usa.

Cookies en `.env`:
    NP_COOKIE_ASPNET_SESSIONID
    NP_COOKIE_UZMX
    NP_COOKIE_UZMXJ

Las cookies expiran en horas. Si el script falla con HTTP 200 + redirect a
login en la respuesta, hay que refrescar las cookies.

Modos de uso
------------
1. Verificar autenticación y descubrir contexto (curso/grupo/instrumentos):
       python notasparciales_upload.py probe \
           --ano 2026 --pac 3 --tipo O --escuela 03 --catedra 253 \
           --encargado DOCENTE_EJEMPLO --tutor 0000000001 \
           --asignatura 00000 --cu 42 --grupo 1 --modelo 4

2. Subir UNA nota de prueba (siempre con --dry-run primero):
       python notasparciales_upload.py single \
           --cedula 0000000002 --instrumento Tar1 --nota 8.9 \
           --ano 2026 --pac 3 --tipo O ... --dry-run

3. Subir un CSV (formato: cedula,instrumento,nota[,observacion_codigo]):
       python notasparciales_upload.py csv --upload-csv notas.csv \
           --ano 2026 --pac 3 --tipo O ... --dry-run

Filosofía
---------
- Por defecto NO escribe nada al servidor: hay que pasar explícitamente
  `--commit` para confirmar (o quitar `--dry-run` si ya se hizo `--commit`).
  Se prefiere requerir `--commit` por seguridad.
- Antes de cada `actualizarNotas`, el script llama a `funObtUltimoCambioNota`
  para detectar si la nota ya existe y exige justificación. Si se detecta esa
  situación y no se proveyó `--justificacion`, el envío se aborta con error
  por defecto (a menos que `--allow-update` sea explícito).
- Cada paso loguea suficiente contexto como para auditarse después.
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Constantes del servidor
# ---------------------------------------------------------------------------
BASE = "https://produccion.uned.ac.cr/notasparciales"
PAGE = f"{BASE}/Formularios/CapturaNotas.aspx"
ORIGIN = "https://produccion.uned.ac.cr"

# Headers que el navegador siempre envía en cada PageMethod call.
COMMON_HEADERS = {
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Content-Type": "application/json; charset=utf-8",
    "X-Requested-With": "XMLHttpRequest",
    "Origin": ORIGIN,
    "Referer": PAGE,
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
    # Imitar Firefox 150 (lo que aparece en el HAR del usuario)
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:150.0) "
        "Gecko/20100101 Firefox/150.0"
    ),
}

# `_peTipo` para el endpoint funCodigoDescripcion (combo loader genérico).
TIPO_PERIODO = 14
TIPO_ESCUELA = 36
TIPO_CATEDRA = 37
TIPO_ENCARGADO = 38
TIPO_TUTOR = 13
TIPO_ASIGNATURA = 8
TIPO_CU = 6
TIPO_GRUPO = 9
TIPO_MODELO = 10
TIPO_TIPO_EVALUACION = 11
TIPO_JUSTIFICACION = 15

logger = logging.getLogger("notasparciales")


def _validate_literal_identifier(label: str, value: str, *, allow_empty: bool = False) -> str:
    """Reject characters that could break legacy pseudo-JavaScript request bodies."""
    text = str(value)
    if not text and allow_empty:
        return text
    if not re.fullmatch(r"[A-Za-z0-9_.@-]+", text):
        raise ValueError(
            f"{label} contiene caracteres no permitidos; use solo letras, "
            "números, punto, guion, guion bajo o @"
        )
    return text


# ---------------------------------------------------------------------------
# Contexto y configuración
# ---------------------------------------------------------------------------
@dataclass
class Context:
    """
    Identifica unívocamente la pantalla de captura de notas que el usuario
    seleccionó en la UI.

    Mapea 1:1 con los filtros visibles en la página antes de cargar la tabla.
    """

    ano: str           # "2026"
    pac: str           # "3"
    tipo: str          # "O" (Ordinaria)
    escuela: str       # "03"
    catedra: int       # 253
    encargado: str     # "DOCENTE_EJEMPLO"
    tutor: str         # cédula ficticia del tutor: "0000000001"
    asignatura: str    # sigla ficticia: "00000"
    cu: str            # centro universitario: "42"
    grupo: int         # 1
    modelo: int        # 4
    evalua: str = "99999"  # "99999" = todos los tipos de evaluación
    col_fin_base: int = 12
    nota_aprobar: int = 7  # default; el sistema la confirma con wmConsultarNotaMinima
    usuario_cedula: str = ""
    usuario_role: int = 14


@dataclass
class Auth:
    """Credenciales de acceso a notasparciales.

    El sitio usa DOS capas de autenticación apiladas:

    1. **NTLM** (Windows Integrated Auth a nivel IIS). Se autentica con el
       usuario UNED del SSO (NO la cédula, sino el username de email).
    2. **Cookies de sesión ASP.NET** (`ASP.NET_SessionId`) más cookies del
       WAF Imperva (`uzmx`, `uzmxj`). Se obtienen del navegador después
       de loguearse en la página.

    Ambas son necesarias: NTLM resuelve el handshake inicial con IIS, y las
    cookies mantienen el estado de la sesión ASP.NET (que recuerda quién
    está logueado y qué grupo/curso está viendo).
    """

    aspnet_sessionid: str
    uzmx: str
    uzmxj: str
    ntlm_user: str = ""        # ej. "jperez"
    ntlm_password: str = ""    # password del SSO UNED

    def to_cookie_dict(self) -> dict[str, str]:
        return {
            "ASP.NET_SessionId": self.aspnet_sessionid,
            "uzmx": self.uzmx,
            "uzmxj": self.uzmxj,
        }


# ---------------------------------------------------------------------------
# Constantes del dominio (descubiertas vía análisis del HAR del navegador)
# ---------------------------------------------------------------------------
NOTA_NO_NOTA = 999      # marcador "sin nota cargada" del servidor
NOTA_NO_PRESENTO = 998  # marcador "no presentó / no entregó la evaluación"
NOTA_RETIRADO = 994     # marcador "retiro justificado"

# Constantes específicas del flujo "no presentó".
TIPO_NOTA_REGULAR = 3
TIPO_NOTA_NO_PRESENTO = 1
OBSERVACION_CODIGO_NO_PRESENTO = 998
NOTA_PLACEHOLDER_NO_PRESENTO = 0.02   # valor numérico arbitrario que usa la UI; el server lo ignora porque TipoNota=1
JUSTIFICACION_NO_PRESENTO = "NO PRESENTÓ O ENTREGÓ LA EVALUACION"


# ---------------------------------------------------------------------------
# Cliente HTTP
# ---------------------------------------------------------------------------
class NotasParcialesClient:
    """Cliente de bajo nivel para los PageMethods de CapturaNotas.aspx."""

    def __init__(self, auth: Auth, *, timeout: float = 30.0):
        self.session = requests.Session()
        self.session.cookies.update(auth.to_cookie_dict())
        self.session.headers.update(COMMON_HEADERS)
        self.timeout = timeout
        # Autenticación NTLM a nivel IIS. Sin esto el server responde 401
        # con WWW-Authenticate: Negotiate/NTLM antes de mirar las cookies.
        if auth.ntlm_user and auth.ntlm_password:
            try:
                from requests_ntlm import HttpNtlmAuth
            except ImportError as ex:  # pragma: no cover
                raise SystemExit(
                    "Falta la dependencia `requests-ntlm`. Instalala con:\n"
                    "    pip install requests-ntlm\n"
                    f"(detalle: {ex})"
                ) from ex
            self.session.auth = HttpNtlmAuth(auth.ntlm_user, auth.ntlm_password)

    # -- Helpers ----------------------------------------------------------
    def _post_json_string(self, method: str, raw_body: str) -> dict[str, Any]:
        """
        POST a `<PAGE>/<method>` con `raw_body` como literal de bytes.

        Importante: muchas llamadas usan literales de objeto JS (claves SIN
        comillas, ej. `{_peAno: '2026'}`) que NO son JSON estándar. ASP.NET
        las acepta porque internamente parsea con un deserializador permisivo.
        Para reproducir EXACTAMENTE lo que hace el navegador, mandamos el
        string crudo (en lugar de json.dumps) y dejamos que el server haga
        su parseo.
        """
        url = f"{PAGE}/{method}"
        logger.debug("POST %s  body=%s", url, raw_body[:300])
        resp = self.session.post(url, data=raw_body.encode("utf-8"), timeout=self.timeout)
        self._check_response(method, resp)
        return self._unwrap_d(resp.json())

    def _post_json(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST a `<PAGE>/<method>` con JSON estándar serializado por nosotros."""
        url = f"{PAGE}/{method}"
        body = json.dumps(payload, ensure_ascii=False)
        logger.debug("POST %s  body=%s", url, body[:300])
        resp = self.session.post(url, data=body.encode("utf-8"), timeout=self.timeout)
        self._check_response(method, resp)
        return self._unwrap_d(resp.json())

    @staticmethod
    def _check_response(method: str, resp: requests.Response) -> None:
        if resp.status_code != 200:
            raise RuntimeError(
                f"{method}: HTTP {resp.status_code}: {resp.text[:500]}"
            )
        ctype = resp.headers.get("Content-Type", "")
        if "json" not in ctype.lower():
            preview = resp.text[:300].replace("\n", " ")
            raise RuntimeError(
                f"{method}: respuesta no-JSON (Content-Type={ctype!r}). "
                "Probable expiración de cookies. Refrescá las 3 cookies en .env. "
                f"Preview: {preview!r}"
            )

    @staticmethod
    def _unwrap_d(envelope: dict[str, Any]) -> dict[str, Any] | str | int | float | None:
        """
        ASP.NET envuelve toda respuesta de PageMethod como `{"d": <payload>}`.
        Algunos métodos devuelven el payload como objeto, otros como string
        JSON-encoded (doble serialización). Acá deserializamos si es string.
        """
        if "d" not in envelope:
            return envelope
        d = envelope["d"]
        if isinstance(d, str):
            try:
                return json.loads(d)
            except json.JSONDecodeError:
                return d
        return d

    # -- Llamadas de alto nivel ------------------------------------------
    def validar_ingreso_notas(self, *, servidor: int = 2) -> Any:
        body = f"{{_peServidor: {servidor}}}"
        return self._post_json_string("funValidarIngresoNotas", body)

    def consultar_nota_minima(self, sigla: str, tipo_mat: str = "O") -> str:
        return self._post_json(
            "wmConsultarNotaMinima",
            {"siglaAsignatura": sigla, "tipoMatricula": tipo_mat},
        )  # type: ignore[return-value]

    def cargar_notas(self, ctx: Context, *, solo_pendientes: str = "N") -> list[dict[str, Any]]:
        _validate_literal_identifier("solo_pendientes", solo_pendientes)
        body = (
            "{_peTipoRetorno: 1,"
            f" _peAno: '{ctx.ano}', _pePAC: '{ctx.pac}', _peTipo: '{ctx.tipo}',"
            f" _peEscuela: '{ctx.escuela}', _peCatedra: {ctx.catedra},"
            f" _peEncargado: '{ctx.encargado}', _peTutor: '{ctx.tutor}',"
            f" _peAsignatura: '{ctx.asignatura}', _peCU: '{ctx.cu}',"
            f" _peGrupo: {ctx.grupo}, _peModelo: {ctx.modelo},"
            f" _peEvalua: '{ctx.evalua}', _peColFinBase: {ctx.col_fin_base},"
            f" _peNotaAprobar: {ctx.nota_aprobar}, _peSoloPendientes: '{solo_pendientes}'"
            "}"
        )
        result = self._post_json_string("funCargarNotas", body)
        if not isinstance(result, dict):
            raise RuntimeError(f"funCargarNotas: respuesta inesperada {result!r}")
        return result.get("Tabla_Datos", [])

    def cargar_nota_individual(self, ctx: Context, cedula: str) -> dict[str, Any]:
        _validate_literal_identifier("cédula", cedula)
        body = (
            "{_peTipoRetorno: 1,"
            f" _peAno: '{ctx.ano}', _pePAC: '{ctx.pac}', _peTipo: '{ctx.tipo}',"
            f" _peEscuela: '{ctx.escuela}', _peCatedra: {ctx.catedra},"
            f" _peEncargado: '{ctx.encargado}', _peTutor: '{ctx.tutor}',"
            f" _peAsignatura: '{ctx.asignatura}', _peCU: '{ctx.cu}',"
            f" _peGrupo: {ctx.grupo}, _peModelo: {ctx.modelo},"
            f" _peEvalua: '{ctx.evalua}', _peColFinBase: {ctx.col_fin_base},"
            f" _peNotaAprobar: {ctx.nota_aprobar}, _peSoloPendientes: 'N',"
            f" cedula: '{cedula}'"
            "}"
        )
        result = self._post_json_string("funCargarNotasIndividual", body)
        if not isinstance(result, dict):
            raise RuntimeError(f"funCargarNotasIndividual: respuesta inesperada {result!r}")
        rows = result.get("Tabla_Datos", [])
        if not rows:
            raise RuntimeError(f"Estudiante {cedula} no encontrado en el grupo")
        return rows[0]

    def obtener_instrumentos_modelo(self, ctx: Context) -> dict[str, Any]:
        body = (
            "{ "
            f" _peAno: '{ctx.ano}', _pePAC: '{ctx.pac}', _peTipo: '{ctx.tipo}',"
            f" _peAsignatura: '{ctx.asignatura}', _peCU: '{ctx.cu}',"
            f" _peGrupo: {ctx.grupo}, _peModelo: {ctx.modelo},"
            f" _peEvalua: '{ctx.evalua}', _peColFinBase: {ctx.col_fin_base}"
            "}"
        )
        result = self._post_json_string("funObtenerInstrumentosModelo", body)
        if not isinstance(result, dict):
            raise RuntimeError(f"funObtenerInstrumentosModelo: respuesta inesperada {result!r}")
        return result

    def obtener_ultimo_cambio(
        self, ctx: Context, cedula: str, instrumento: str, *, es_reposicion: int = 0
    ) -> dict[str, Any]:
        _validate_literal_identifier("cédula", cedula)
        _validate_literal_identifier("instrumento", instrumento)
        body = (
            "{"
            f"_peAno: '{ctx.ano}', _pePAC: '{ctx.pac}', _peTipo: '{ctx.tipo}',"
            f" _peEvalua: '{instrumento}', _peCedula: '{cedula}',"
            f" _peModelo: {ctx.modelo}, _peAsignatura: '{ctx.asignatura}',"
            f" _peEsReposicion: {es_reposicion}"
            "}"
        )
        result = self._post_json_string("funObtUltimoCambioNota", body)
        if not isinstance(result, dict):
            raise RuntimeError(f"funObtUltimoCambioNota: respuesta inesperada {result!r}")
        return result

    def obtener_estado_sae(self, ctx: Context, cedula: str) -> dict[str, Any]:
        _validate_literal_identifier("cédula", cedula)
        body = (
            "{ _peModo_Unico: false,"
            f" _peAno: '{ctx.ano}', _pePAC: '{ctx.pac}', _peTipo: '{ctx.tipo}',"
            f" _peEscuela: '{ctx.escuela}', _peCatedra: {ctx.catedra},"
            f" _peEncargado: '{ctx.encargado}', _peTutor: '{ctx.tutor}',"
            f" _peAsignatura: '{ctx.asignatura}', _peCU: '{ctx.cu}',"
            f" _peGrupo: {ctx.grupo}, _peModelo: {ctx.modelo},"
            f" _peCedula: '{cedula}'"
            "}"
        )
        result = self._post_json_string("funObtener_SAE_Estado_Actual_UNICO", body)
        if not isinstance(result, dict):
            raise RuntimeError(f"funObtener_SAE_Estado_Actual_UNICO: respuesta inesperada {result!r}")
        return result

    def actualizar_notas(
        self,
        ctx: Context,
        cedula: str,
        instrumento_codigo: str,
        instrumento_nombre: str,
        nota: float | str | None = None,
        *,
        no_presento: bool = False,
        observacion_codigo: int = 0,
        observacion_descripcion: str = "",
        justificacion: str = "",
        tipo_nota: int = TIPO_NOTA_REGULAR,
    ) -> dict[str, Any]:
        """
        Llama al endpoint `actualizarNotas`. Reproduce EXACTAMENTE la forma del
        payload que envía el navegador (capturada del HAR).

        Dos modos:
          - `no_presento=False` (default): carga una nota numérica.
            * `nota` se envía como STRING con un decimal (formato "8.9").
            * `TipoNota=3` (regular).
          - `no_presento=True`: marca el instrumento como "no presentó".
            * El parámetro `nota` se IGNORA (la UI manda 0.02 como placeholder).
            * `TipoNota=1`, `observacion.Codigo=998`, justificación literal
              "NO PRESENTÓ O ENTREGÓ LA EVALUACION".
            * Cualquier `observacion_codigo` / `justificacion` que se pase
              en este modo es sobrescrito por los valores fijos.
        """
        if no_presento:
            nota_value: Any = NOTA_PLACEHOLDER_NO_PRESENTO
            tipo_nota = TIPO_NOTA_NO_PRESENTO
            observacion_codigo = OBSERVACION_CODIGO_NO_PRESENTO
            justificacion = JUSTIFICACION_NO_PRESENTO
            observacion_descripcion = ""
        else:
            if nota is None:
                raise ValueError("nota es obligatoria cuando no_presento=False")
            nota_value = self._format_nota(nota)

        payload = {
            "grupo": {
                "Numero": str(ctx.grupo),
                "CentroUniversitario": {"Codigo": ctx.cu, "Nombre": ""},
                "ListaPromedios": [
                    {
                        "Estudiante": {"Cedula": cedula},
                        "Grupo": None,
                        "Modelo": None,
                        "pac": 0,
                        "Anno": 0,
                        "NotaReposicion1": 0,
                        "NotaReposicion2": 0,
                        "NotaReposicion3": 0,
                        "PromedioParcial": 0,
                        "PromedioFinal": 0,
                        "ListaNotas": [
                            {
                                "TipoNota": tipo_nota,
                                "Nota": nota_value,
                                "PromedioEstudiante": None,
                                "InstrumentoEvaluacion": {
                                    "Codigo": instrumento_codigo,
                                    "Nombre": instrumento_nombre,
                                },
                            }
                        ],
                        "Estado": None,
                        "Condicion": 0,
                    }
                ],
                "Modelo": {
                    "Numero": str(ctx.modelo),
                    "Curso": {
                        "Sigla": ctx.asignatura,
                        "Anno": ctx.ano,
                        "Pac": ctx.pac,
                        "TipoMatricula": ctx.tipo,
                    },
                    "Instrumentos": [],
                },
            },
            "observacion": {
                "Codigo": observacion_codigo,
                "Descripcion": observacion_descripcion,
                "Activo": True,
                # NOTA: en el HAR de "no presentó", NecesitaJustificar=false aunque
                # la justificación tenga texto. Replicamos eso al pie de la letra.
                "NecesitaJustificar": False if no_presento else bool(justificacion),
                "PermiteCambiosPosteriores": True,
            },
            "justificacion": justificacion,
        }
        return self._post_json("actualizarNotas", payload)  # type: ignore[return-value]

    @staticmethod
    def _format_nota(value: float | str) -> str:
        """
        Formatea una nota como string con UN decimal. La UI permite enteros y
        un decimal; replicamos eso. Acepta str para no romper si el caller ya
        envió "8.9".
        """
        if isinstance(value, str):
            value = value.replace(",", ".").strip()
            try:
                value = float(value)
            except ValueError:
                raise ValueError(f"Nota inválida: {value!r}")
        if value < 0 or value > 10:
            raise ValueError(f"Nota fuera de rango [0, 10]: {value}")
        return f"{value:.1f}"


# ---------------------------------------------------------------------------
# Lógica de aplicación
# ---------------------------------------------------------------------------
def _load_auth_and_context_from_env(args: argparse.Namespace) -> tuple[Auth, Context]:
    load_dotenv()
    auth = Auth(
        aspnet_sessionid=os.environ.get("NP_COOKIE_ASPNET_SESSIONID", "").strip(),
        uzmx=os.environ.get("NP_COOKIE_UZMX", "").strip(),
        uzmxj=os.environ.get("NP_COOKIE_UZMXJ", "").strip(),
        ntlm_user=os.environ.get("NP_NTLM_USER", "").strip(),
        ntlm_password=os.environ.get("NP_NTLM_PASSWORD", ""),
    )
    missing_cookies = [
        k for k in ("aspnet_sessionid", "uzmx", "uzmxj")
        if not getattr(auth, k)
    ]
    if missing_cookies:
        raise SystemExit(
            "Faltan cookies en .env: "
            + ", ".join(f"NP_COOKIE_{k.upper()}" for k in missing_cookies)
            + "\nVer .env.example para instrucciones de cómo obtenerlas."
        )
    if not auth.ntlm_user or not auth.ntlm_password:
        raise SystemExit(
            "Faltan credenciales NTLM en .env: NP_NTLM_USER y/o NP_NTLM_PASSWORD.\n"
            "El sitio /notasparciales/ exige autenticación NTLM ANTES de aceptar cookies.\n"
            "Usá tu username del SSO UNED (ej. `jperez`, NO la cédula) y tu password.\n"
            "Ver .env.example."
        )

    usuario_cedula = args.usuario_cedula or os.environ.get("NP_USUARIO_CEDULA", "")
    usuario_role = int(args.usuario_role or os.environ.get("NP_USUARIO_ROLE", "14"))

    ctx = Context(
        ano=args.ano,
        pac=args.pac,
        tipo=args.tipo,
        escuela=args.escuela,
        catedra=args.catedra,
        encargado=args.encargado,
        tutor=args.tutor,
        asignatura=args.asignatura,
        cu=args.cu,
        grupo=args.grupo,
        modelo=args.modelo,
        usuario_cedula=usuario_cedula,
        usuario_role=usuario_role,
    )
    for field_name in (
        "ano", "pac", "tipo", "escuela", "encargado", "tutor",
        "asignatura", "evalua", "usuario_cedula",
    ):
        _validate_literal_identifier(
            field_name,
            getattr(ctx, field_name),
            allow_empty=field_name == "usuario_cedula",
        )
    _validate_literal_identifier("cu", ctx.cu, allow_empty=True)
    return auth, ctx


def _detect_session_dead(client: NotasParcialesClient) -> None:
    """
    Hace una llamada barata para detectar cookies muertas. Si pasa, OK.
    Si falla, levanta error con mensaje claro.
    """
    try:
        client.validar_ingreso_notas()
    except RuntimeError as e:
        raise SystemExit(
            "No se pudo validar la sesión con notasparciales.\n"
            f"Detalle: {e}\n"
            "Refrescá las 3 cookies (NP_COOKIE_*) en .env y volvé a intentar."
        ) from e


def cmd_probe(args: argparse.Namespace) -> int:
    auth, ctx = _load_auth_and_context_from_env(args)
    client = NotasParcialesClient(auth)

    print("== Probando autenticación ==")
    _detect_session_dead(client)
    print("OK: cookies válidas, el sistema está abierto.")

    print(f"\n== Nota mínima para {ctx.asignatura} ==")
    nota_min = client.consultar_nota_minima(ctx.asignatura, ctx.tipo)
    print(f"Nota mínima de aprobación: {nota_min}")

    print("\n== Instrumentos del modelo ==")
    instr = client.obtener_instrumentos_modelo(ctx)
    encabezados = [h.get("Dato", "") for h in instr.get("Tabla_Encabezados", [])]
    columnas = instr.get("Tabla_Modelo", [])
    print("Encabezados visibles en la tabla:")
    for e in encabezados:
        print(f"  - {e}")
    print("\nMapeo Codigo -> Nombre del instrumento (lo que necesitás para --instrumento):")
    for col in columnas:
        name = col.get("name", "")
        index = col.get("index", "")
        if not name or name in METADATA_COLUMNS:
            continue
        print(f"  {name:8s}  ->  {index}")

    print("\n== Cargando tabla del grupo (resumen) ==")
    rows = client.cargar_notas(ctx)
    print(f"Estudiantes en el grupo: {len(rows)}")
    for r in rows[:5]:
        nombre = r.get("Nombre", "").strip()
        cedula = r.get("Cedula", "")
        promedio = r.get("Promedio", 0)
        print(f"  {cedula}  {nombre[:40]:40s}  promedio={promedio}")
    if len(rows) > 5:
        print(f"  ... ({len(rows) - 5} más)")

    return 0


def _resolve_instrument_name(client: NotasParcialesClient, ctx: Context, codigo: str) -> str:
    """Busca el `Nombre` legible del instrumento (ej. 'Tarea 1 (2)')."""
    instr = client.obtener_instrumentos_modelo(ctx)
    encabezados = instr.get("Tabla_Encabezados", [])
    columnas = instr.get("Tabla_Modelo", [])

    # `Tabla_Modelo` y `Tabla_Encabezados` están alineados por índice; la
    # columna del instrumento tiene `name` = código (Tar1, Proy1) y el
    # encabezado correspondiente tiene `Dato` = "Tarea 1 <br> (2)" o similar.
    for i, col in enumerate(columnas):
        if col.get("name") == codigo:
            if i < len(encabezados):
                raw = encabezados[i].get("Dato", "")
                # El encabezado típicamente viene como "Tarea 1 <br> (2)".
                # Lo normalizamos a "Tarea 1 (2)" (tal cual lo manda el browser
                # en `Nombre` en actualizarNotas).
                return raw.replace("<br>", "").replace("\u003cbr\u003e", "").replace("  ", " ").strip()
            break
    raise ValueError(
        f"Instrumento {codigo!r} no existe en este modelo. "
        "Corrélo con --probe para ver los códigos disponibles."
    )


def _upload_one(
    client: NotasParcialesClient,
    ctx: Context,
    cedula: str,
    instrumento: str,
    nota: float | str | None,
    *,
    no_presento: bool = False,
    instrumento_nombre: str | None = None,
    justificacion_codigo: int = 0,
    justificacion_texto: str = "",
    allow_update: bool = False,
    dry_run: bool = True,
) -> dict[str, Any]:
    nombre_str = instrumento_nombre or _resolve_instrument_name(client, ctx, instrumento)

    cambio = client.obtener_ultimo_cambio(ctx, cedula, instrumento)
    permite = cambio.get("PermiteCambio", False)
    tiene = cambio.get("TieneCambios", False)
    msg = cambio.get("Mensaje", "")
    logger.info(
        "  funObtUltimoCambioNota: PermiteCambio=%s TieneCambios=%s Mensaje=%r",
        permite, tiene, msg,
    )
    if not permite:
        raise RuntimeError(
            f"El servidor no permite el cambio de {cedula}/{instrumento}: {msg}"
        )
    if tiene and not allow_update:
        raise RuntimeError(
            f"{cedula}/{instrumento} ya tiene cambios previos. "
            "Pasá --allow-update y --justificacion-codigo para sobrescribir."
        )

    needs_justif = tiene and not no_presento  # 'no presentó' lleva su propia justificación fija
    payload_preview: dict[str, Any]
    if no_presento:
        payload_preview = {
            "cedula": cedula,
            "instrumento": instrumento,
            "instrumento_nombre": nombre_str,
            "accion": "MARCAR NO PRESENTÓ",
        }
    else:
        payload_preview = {
            "cedula": cedula,
            "instrumento": instrumento,
            "instrumento_nombre": nombre_str,
            "nota": NotasParcialesClient._format_nota(nota),  # type: ignore[arg-type]
            "justificacion_codigo": justificacion_codigo if needs_justif else 0,
            "justificacion_texto": justificacion_texto if needs_justif else "",
        }
    logger.info("  -> payload: %s", payload_preview)

    if dry_run:
        logger.info("  [dry-run] no se envía actualizarNotas")
        return {"d": "dry-run", "payload_preview": payload_preview}

    resp = client.actualizar_notas(
        ctx,
        cedula=cedula,
        instrumento_codigo=instrumento,
        instrumento_nombre=nombre_str,
        nota=nota,
        no_presento=no_presento,
        observacion_codigo=justificacion_codigo if needs_justif else 0,
        observacion_descripcion="",
        justificacion=justificacion_texto if needs_justif else "",
    )
    logger.info("  <- respuesta: %s", resp)
    if isinstance(resp, dict) and resp.get("HayError"):
        raise RuntimeError(f"actualizarNotas reportó error: {resp.get('Descripcion')}")
    return resp


def cmd_single(args: argparse.Namespace) -> int:
    auth, ctx = _load_auth_and_context_from_env(args)
    client = NotasParcialesClient(auth)
    _detect_session_dead(client)

    logger.info("Subiendo: cedula=%s instrumento=%s nota=%s dry_run=%s",
                args.cedula, args.instrumento, args.nota, args.dry_run)
    _upload_one(
        client, ctx,
        cedula=args.cedula,
        instrumento=args.instrumento,
        nota=args.nota,
        justificacion_codigo=args.justificacion_codigo,
        justificacion_texto=args.justificacion_texto,
        allow_update=args.allow_update,
        dry_run=args.dry_run,
    )

    if not args.dry_run:
        actual = client.cargar_nota_individual(ctx, args.cedula)
        valor = actual.get(args.instrumento, "?")
        print(f"\nVerificación: {args.cedula} / {args.instrumento} = {valor}")
    return 0


def cmd_upload_csv(args: argparse.Namespace) -> int:
    auth, ctx = _load_auth_and_context_from_env(args)
    client = NotasParcialesClient(auth)
    _detect_session_dead(client)

    csv_path = Path(args.upload_csv)
    if not csv_path.exists():
        raise SystemExit(f"CSV no encontrado: {csv_path}")

    logger.info("Procesando CSV: %s", csv_path)
    rows: list[dict[str, str]] = []
    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        required = {"cedula", "instrumento", "nota"}
        if not required.issubset({c.lower() for c in (reader.fieldnames or [])}):
            raise SystemExit(
                f"El CSV debe tener columnas: {sorted(required)}. "
                f"Encontradas: {reader.fieldnames}"
            )
        for r in reader:
            rows.append({k.lower(): (v or "").strip() for k, v in r.items()})

    print(f"Filas a procesar: {len(rows)} (dry_run={args.dry_run})")
    ok = 0
    failed: list[tuple[dict[str, str], str]] = []
    for i, row in enumerate(rows, 1):
        try:
            print(f"\n[{i}/{len(rows)}] {row['cedula']} / {row['instrumento']} = {row['nota']}")
            _upload_one(
                client, ctx,
                cedula=row["cedula"],
                instrumento=row["instrumento"],
                nota=row["nota"],
                justificacion_codigo=int(row.get("observacion_codigo") or 0),
                justificacion_texto=row.get("justificacion") or "",
                allow_update=args.allow_update,
                dry_run=args.dry_run,
            )
            ok += 1
        except Exception as e:  # noqa: BLE001
            logger.error("FALLO: %s", e)
            failed.append((row, str(e)))
        finally:
            time.sleep(args.delay)  # rate-limit cortés

    print(f"\n== Resumen ==\nOK:    {ok}\nFAIL:  {len(failed)}")
    if failed:
        print("\nFallidos:")
        for row, err in failed:
            print(f"  {row['cedula']} / {row['instrumento']} -> {err}")
    return 0 if not failed else 1


# ---------------------------------------------------------------------------
# Plan + Apply: ingesta de xlsx, diff contra servidor, ejecución
# ---------------------------------------------------------------------------
# Nombres de columnas que vienen en Tabla_Modelo pero NO son instrumentos
# de evaluación. Cualquier `name` que no esté en este set se considera un
# instrumento (Tar1, Tar2, Proy1, etc.).
METADATA_COLUMNS = frozenset({
    "Tipo", "Cedula", "Nombre", "Promedio", "Decimales", "Redondeado",
    "Condicion", "Acta", "ActaTXT", "Situacion", "SituacionTXT", "Sep1",
})

# Acciones del plan (van en la columna `accion` del CSV)
ACCION_UPLOAD = "upload"
ACCION_MARK_NOT_PRESENTED = "mark_not_presented"  # '-' en xlsx → marcar como "no presentó"
ACCION_SKIP_ALREADY = "skip_already_set"          # ya existe el mismo estado en el sistema
ACCION_SKIP_RETIRADO = "skip_retirado"            # estudiante con marcador 994 (retiro)
ACCION_SKIP_NOT_IN_ROSTER = "skip_not_in_roster"  # cédula del xlsx no aparece en el grupo
ACCION_WOULD_OVERWRITE = "would_overwrite"        # estado local difiere de remoto; requiere --allow-update
ACCION_REVIEW = "review"                          # caso ambiguo que conviene revisar a mano


@dataclass
class XlsxEntry:
    """Una nota leída del xlsx, ya normalizada."""

    cedula: str
    nombre: str
    cu_xlsx: str       # "01" o "42" (de la columna Institución)
    column_header: str  # encabezado original del xlsx
    nota_raw: str       # "89", "100", "-", etc. tal cual venía
    nota: float | None  # None si era '-' o no parseable
    fuente: str         # nombre de archivo


def _parse_xlsx(path: Path) -> tuple[list[str], list[list[Any]]]:
    """Devuelve (encabezados, filas) del xlsx. Lazy-import para no obligar a openpyxl en otros modos."""
    try:
        import openpyxl  # type: ignore
    except ImportError:
        raise SystemExit(
            "openpyxl no está instalado. Corré:  pip install openpyxl"
        )
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    ws = wb[wb.sheetnames[0]]
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        raise SystemExit(f"{path.name}: archivo vacío")
    headers = [str(h) if h is not None else "" for h in rows[0]]
    return headers, [list(r) for r in rows[1:]]


_INSTITUCION_RE = re.compile(r"\((\d{2,3})\)\s*$")


def _extract_cu_from_institucion(value: str) -> str | None:
    """De 'SAN JOSE (01)' devuelve '01'."""
    if not value:
        return None
    m = _INSTITUCION_RE.search(str(value))
    return m.group(1) if m else None


def _normalize_for_match(s: str) -> str:
    """Lowercase + colapsa espacios + quita ruido común para comparar nombres de instrumentos."""
    s = s.lower()
    # quitar prefijos típicos de Moodle
    s = re.sub(r"\btarea\s*:\s*", "", s)
    s = re.sub(r"\bentrega\s+actividad\b", "", s)
    # quitar marcadores entre paréntesis "(real)", "(porcentaje)", "(2)", etc.
    s = re.sub(r"\(.*?\)", "", s)
    # quitar etiquetas html sueltas
    s = re.sub(r"<[^>]+>", " ", s)
    # quitar palabras de relleno
    s = re.sub(r"\bde\b", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _is_grade_column(header: str) -> bool:
    """Identifica columnas de Moodle que contienen una nota (heurística simple)."""
    if not header:
        return False
    h = header.lower()
    if "(real)" in h or "(porcentaje)" in h:
        return True
    if "última descarga" in h or "ultima descarga" in h:
        return False
    return False


def _ingest_xlsx_files(paths: list[Path]) -> tuple[list[XlsxEntry], list[str]]:
    """
    Lee uno o más xlsx (formato exportación Moodle: 'Calificaciones').
    Devuelve (entries, encabezados_de_columnas_de_nota).
    """
    entries: list[XlsxEntry] = []
    grade_headers: list[str] = []
    seen = set()  # (cedula, header) para evitar duplicados entre archivos

    for p in paths:
        headers, rows = _parse_xlsx(p)
        try:
            i_nombre = headers.index("Nombre")
            i_apellido = headers.index("Apellido(s)")
            i_id = headers.index("Número de ID")
            i_inst = headers.index("Institución")
        except ValueError as e:
            raise SystemExit(
                f"{p.name}: no se encontraron las columnas esperadas de Moodle: {e}"
            )

        # Identificar columnas de nota (las que terminan en (Real) o (Porcentaje))
        grade_cols = [(i, h) for i, h in enumerate(headers) if _is_grade_column(h)]
        for _, h in grade_cols:
            if h not in grade_headers:
                grade_headers.append(h)

        for r in rows:
            if not r or all(v is None for v in r):
                continue
            try:
                nombre = (str(r[i_nombre] or "") + " " + str(r[i_apellido] or "")).strip()
                cedula = str(r[i_id] or "").strip()
                cu = _extract_cu_from_institucion(str(r[i_inst] or "")) or ""
            except IndexError:
                continue
            if not cedula:
                continue
            for col_idx, header in grade_cols:
                raw = r[col_idx] if col_idx < len(r) else None
                raw_str = "" if raw is None else str(raw).strip()
                key = (cedula, header)
                if key in seen:
                    logger.warning(
                        "Duplicado: %s / %r aparece en %s y se ignora.",
                        cedula, header, p.name,
                    )
                    continue
                seen.add(key)
                nota: float | None
                if raw_str in ("", "-"):
                    nota = None
                else:
                    try:
                        nota = float(raw_str.replace(",", "."))
                    except ValueError:
                        logger.warning(
                            "Nota no parseable %r para %s (%s) en %s, ignorada.",
                            raw_str, cedula, header, p.name,
                        )
                        nota = None
                entries.append(XlsxEntry(
                    cedula=cedula,
                    nombre=nombre,
                    cu_xlsx=cu,
                    column_header=header,
                    nota_raw=raw_str,
                    nota=nota,
                    fuente=p.name,
                ))
    return entries, grade_headers


def _build_instrument_mapping(
    grade_headers: list[str],
    server_columns: list[dict[str, Any]],
    server_encabezados: list[dict[str, Any]],
    explicit_map: dict[str, str] | None = None,
) -> dict[str, tuple[str, str]]:
    """
    Mapea cada encabezado de xlsx a (codigo, nombre_legible) del instrumento en notasparciales.

    Estrategia:
    1. Si `explicit_map` mapea esa columna explícitamente, usarla.
    2. Normalizar ambos lados y match por substring exacto.
    3. Match relajado: el lado xlsx contiene al lado servidor o viceversa.

    Devuelve {column_header: (codigo, nombre_legible)} para columnas matcheadas.
    Las que no matcheen no aparecen (el caller debe reportarlo).
    """
    # Construir lista de candidatos del servidor: [(codigo, nombre_legible)].
    # IMPORTANTE: el campo `editable` que devuelve el server es False incluso
    # para los instrumentos de nota (Tar1, Proy1, etc.). NO usarlo como filtro.
    # Lo único confiable es el `name` y descartar las columnas de metadatos.
    server_candidates: list[tuple[str, str]] = []
    for col, enc in zip(server_columns, server_encabezados):
        codigo = col.get("name") or ""
        if not codigo or codigo in METADATA_COLUMNS:
            continue
        nombre_legible = (
            (enc.get("Dato") or "")
            .replace("<br>", " ")
            .replace("\u003cbr\u003e", " ")
        )
        nombre_legible = re.sub(r"\s+", " ", nombre_legible).strip()
        server_candidates.append((codigo, nombre_legible))

    explicit_map = explicit_map or {}
    mapping: dict[str, tuple[str, str]] = {}
    used_codes: set[str] = set()

    # Pase 1: --map explícito
    for header, code in explicit_map.items():
        for codigo, nombre in server_candidates:
            if codigo == code:
                mapping[header] = (codigo, nombre)
                used_codes.add(codigo)
                break
        else:
            raise SystemExit(
                f"--map dice {header!r}={code!r}, pero ese código no existe en el modelo"
            )

    # Pase 2: substring exacto sobre la versión normalizada
    for header in grade_headers:
        if header in mapping:
            continue
        h_norm = _normalize_for_match(header)
        for codigo, nombre in server_candidates:
            if codigo in used_codes:
                continue
            n_norm = _normalize_for_match(nombre)
            if h_norm == n_norm or h_norm in n_norm or n_norm in h_norm:
                mapping[header] = (codigo, nombre)
                used_codes.add(codigo)
                break

    # Pase 3: match por "tipo" (palabra clave: tarea, proyecto, examen, etc.)
    # Útil para casos como "proyecto final" (xlsx) ↔ "proyecto 1" (servidor):
    # si queda exactamente UN instrumento por matchear de cada lado y comparten
    # la misma palabra-clave, los unimos.
    type_keywords = ("proyecto", "tarea", "examen", "prueba", "quiz",
                     "trabajo", "ensayo", "investigacion", "lectura", "foro")
    pending_xlsx = [h for h in grade_headers if h not in mapping]
    pending_server = [(c, n) for c, n in server_candidates if c not in used_codes]

    for kw in type_keywords:
        xlsx_with_kw = [h for h in pending_xlsx if kw in _normalize_for_match(h)]
        srv_with_kw = [(c, n) for c, n in pending_server if kw in _normalize_for_match(n)]
        if len(xlsx_with_kw) == 1 and len(srv_with_kw) == 1:
            header = xlsx_with_kw[0]
            codigo, nombre = srv_with_kw[0]
            mapping[header] = (codigo, nombre)
            used_codes.add(codigo)
            pending_xlsx.remove(header)
            pending_server = [(c, n) for c, n in pending_server if c not in used_codes]

    return mapping


def _normalize_nota_local(value: float) -> str:
    """Convierte 0-100 (Moodle) a string 0-10 con 1 decimal (notasparciales)."""
    if value < 0 or value > 100:
        raise ValueError(f"Nota local fuera de [0, 100]: {value}")
    converted = value / 10.0
    return f"{converted:.1f}"


def _is_real_grade(value: Any) -> bool:
    """True si `value` parece una nota cargada (no 999/998/994, no None)."""
    if value is None:
        return False
    try:
        v = float(value)
    except (TypeError, ValueError):
        return False
    return v not in (NOTA_NO_NOTA, NOTA_NO_PRESENTO, NOTA_RETIRADO) and 0 <= v <= 10


def _is_marked_no_presento(value: Any) -> bool:
    """True si la celda del servidor está en 998 ('no presentó')."""
    if value is None:
        return False
    try:
        return float(value) == NOTA_NO_PRESENTO
    except (TypeError, ValueError):
        return False


def _is_retirado(
    student_row: dict[str, Any], instrument_keys: list[str] | None = None
) -> bool:
    """Heurística: si TODOS los instrumentos del estudiante son 994."""
    keys = instrument_keys or [
        key for key in student_row
        if key not in METADATA_COLUMNS and re.fullmatch(r"[A-Za-z]+\d+", key)
    ]
    keys = [key for key in keys if key in student_row]
    if not keys:
        return False
    return all(student_row.get(key) == NOTA_RETIRADO for key in keys)


@dataclass
class PlanRow:
    cu: str
    grupo: int
    cedula: str
    nombre: str
    instrumento: str
    instrumento_nombre: str
    nota_local: str
    nota_remota: str
    accion: str
    motivo: str
    fuente: str


def _fetch_server_state(
    client: NotasParcialesClient,
    base_ctx: Context,
    cu_grupo_pairs: list[tuple[str, int]],
) -> tuple[dict[tuple[str, int], dict[str, dict[str, Any]]],
           dict[tuple[str, int], dict[str, tuple[str, str]]]]:
    """
    Para cada (cu, grupo) trae el roster completo y los instrumentos del modelo.

    Devuelve:
        roster: {(cu, grupo): {cedula: row_dict}}
        instruments: {(cu, grupo): {codigo: (codigo, nombre_legible)}}
    """
    roster: dict[tuple[str, int], dict[str, dict[str, Any]]] = {}
    instruments: dict[tuple[str, int], dict[str, tuple[str, str]]] = {}
    for cu, grupo in cu_grupo_pairs:
        ctx = dataclasses.replace(base_ctx, cu=cu, grupo=grupo)
        rows = client.cargar_notas(ctx)
        roster[(cu, grupo)] = {str(r.get("Cedula", "")): r for r in rows}

        instr_resp = client.obtener_instrumentos_modelo(ctx)
        cols = instr_resp.get("Tabla_Modelo", []) or []
        encs = instr_resp.get("Tabla_Encabezados", []) or []
        instr_map: dict[str, tuple[str, str]] = {}
        for col, enc in zip(cols, encs):
            codigo = col.get("name") or ""
            if not codigo or codigo in METADATA_COLUMNS:
                continue
            nombre = re.sub(r"\s+", " ", (enc.get("Dato") or "").replace("<br>", " ")).strip()
            instr_map[codigo] = (codigo, nombre)
        instruments[(cu, grupo)] = instr_map
    return roster, instruments


def _parse_cu_grupo_args(values: list[str]) -> dict[str, int]:
    """Parsea ['42=1', '01=2'] a {'42': 1, '01': 2}."""
    out: dict[str, int] = {}
    for v in values or []:
        if "=" not in v:
            raise SystemExit(f"--cu-grupo malformado: {v!r} (esperado CU=GRUPO, ej. 01=2)")
        cu, g = v.split("=", 1)
        out[cu.strip()] = int(g.strip())
    return out


def _parse_explicit_map(values: list[str]) -> dict[str, str]:
    """Parsea ['Tarea 1=Tar1', ...] a {'<header>': '<codigo>'} (no acepta wildcards)."""
    out: dict[str, str] = {}
    for v in values or []:
        if "=" not in v:
            raise SystemExit(f"--map malformado: {v!r} (esperado HEADER=CODIGO)")
        header, code = v.split("=", 1)
        out[header.strip()] = code.strip()
    return out


def _format_remote(value: Any) -> str:
    """Formatea el valor remoto para el CSV de plan (legible para auditoría)."""
    if value is None or value == "":
        return ""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    if v == NOTA_NO_NOTA:
        return "999 (vacío)"
    if v == NOTA_NO_PRESENTO:
        return "998 (no presentó)"
    if v == NOTA_RETIRADO:
        return "994 (retiro)"
    if 0 <= v <= 10:
        return f"{v:.1f}"
    return str(value)


def _build_plan(
    entries: list[XlsxEntry],
    cu_grupo_map: dict[str, int],
    instrument_mapping: dict[str, tuple[str, str]],
    roster: dict[tuple[str, int], dict[str, dict[str, Any]]],
) -> list[PlanRow]:
    """
    Cruza los datos del xlsx con el estado del servidor y produce una fila por
    par (estudiante × instrumento). Cada fila tiene una `accion` clara y un
    `motivo` legible.

    Reglas (matriz xlsx × servidor):

        xlsx         | servidor          | acción
        ─────────────┼───────────────────┼───────────────────────
        número       | 999 (vacío)       | upload
        número       | 998 (no presentó) | would_overwrite (review)
        número       | misma nota        | skip_already_set
        número       | otra nota         | would_overwrite
        número       | 994 (retiro)      | skip_retirado
        '-'          | 999 (vacío)       | mark_not_presented
        '-'          | 998 (no presentó) | skip_already_set
        '-'          | nota real         | review (raro: borraría una nota)
        '-'          | 994 (retiro)      | skip_retirado
    """
    plan: list[PlanRow] = []

    for entry in entries:
        cu = entry.cu_xlsx

        if cu not in cu_grupo_map:
            plan.append(PlanRow(
                cu=cu, grupo=0,
                cedula=entry.cedula, nombre=entry.nombre,
                instrumento="", instrumento_nombre="",
                nota_local=entry.nota_raw, nota_remota="",
                accion=ACCION_SKIP_NOT_IN_ROSTER,
                motivo=f"CU {cu!r} no tiene mapeo --cu-grupo",
                fuente=entry.fuente,
            ))
            continue

        grupo = cu_grupo_map[cu]
        key = (cu, grupo)

        if key not in roster:
            plan.append(PlanRow(
                cu=cu, grupo=grupo,
                cedula=entry.cedula, nombre=entry.nombre,
                instrumento="", instrumento_nombre="",
                nota_local=entry.nota_raw, nota_remota="",
                accion=ACCION_SKIP_NOT_IN_ROSTER,
                motivo=f"No se cargó roster para CU={cu} Grupo={grupo}",
                fuente=entry.fuente,
            ))
            continue

        if entry.cedula not in roster[key]:
            plan.append(PlanRow(
                cu=cu, grupo=grupo,
                cedula=entry.cedula, nombre=entry.nombre,
                instrumento="", instrumento_nombre="",
                nota_local=entry.nota_raw, nota_remota="",
                accion=ACCION_SKIP_NOT_IN_ROSTER,
                motivo="Cédula no aparece en el roster oficial del grupo",
                fuente=entry.fuente,
            ))
            continue

        student_row = roster[key][entry.cedula]

        instrument_keys = sorted({code for code, _ in instrument_mapping.values()})
        if _is_retirado(student_row, instrument_keys):
            plan.append(PlanRow(
                cu=cu, grupo=grupo,
                cedula=entry.cedula, nombre=entry.nombre,
                instrumento="", instrumento_nombre="",
                nota_local=entry.nota_raw, nota_remota="994 (retiro)",
                accion=ACCION_SKIP_RETIRADO,
                motivo="Estudiante con marcador de retiro (994)",
                fuente=entry.fuente,
            ))
            continue

        if entry.column_header not in instrument_mapping:
            plan.append(PlanRow(
                cu=cu, grupo=grupo,
                cedula=entry.cedula, nombre=entry.nombre,
                instrumento="?", instrumento_nombre=entry.column_header,
                nota_local=entry.nota_raw, nota_remota="",
                accion=ACCION_REVIEW,
                motivo=f"Columna {entry.column_header!r} sin mapeo a un instrumento del modelo",
                fuente=entry.fuente,
            ))
            continue

        codigo, nombre_legible = instrument_mapping[entry.column_header]
        remota_raw = student_row.get(codigo)
        remota_str = _format_remote(remota_raw)

        # Caso 1: xlsx tiene celda '-'
        if entry.nota is None:
            if _is_marked_no_presento(remota_raw):
                accion = ACCION_SKIP_ALREADY
                motivo = "Ya marcado como 'no presentó' en el sistema (998)"
                nota_local_str = "-"
            elif _is_real_grade(remota_raw):
                accion = ACCION_REVIEW
                motivo = (
                    "El xlsx dice '-' pero el servidor tiene una nota real cargada. "
                    "Revisar manualmente: probablemente NO marcar 'no presentó' aquí."
                )
                nota_local_str = "-"
            else:  # remota es 999 o vacía
                accion = ACCION_MARK_NOT_PRESENTED
                motivo = "Marcar como 'no presentó el instrumento'"
                nota_local_str = "-"
            plan.append(PlanRow(
                cu=cu, grupo=grupo,
                cedula=entry.cedula, nombre=entry.nombre,
                instrumento=codigo, instrumento_nombre=nombre_legible,
                nota_local=nota_local_str, nota_remota=remota_str,
                accion=accion, motivo=motivo,
                fuente=entry.fuente,
            ))
            continue

        # Caso 2: xlsx tiene nota numérica
        nota_local_str = _normalize_nota_local(entry.nota)

        if _is_marked_no_presento(remota_raw):
            accion = ACCION_WOULD_OVERWRITE
            motivo = (
                f"Servidor está en 998 (no presentó) y xlsx tiene nota {nota_local_str}. "
                "Requiere --allow-update y --justificacion-codigo (probablemente 2005)."
            )
        elif _is_real_grade(remota_raw):
            remota_num = f"{float(remota_raw):.1f}"
            if remota_num == nota_local_str:
                accion = ACCION_SKIP_ALREADY
                motivo = "Misma nota ya cargada en el sistema"
            else:
                accion = ACCION_WOULD_OVERWRITE
                motivo = (
                    f"Nota local {nota_local_str} difiere de remota {remota_num}; "
                    "requiere --allow-update y --justificacion-codigo"
                )
        else:  # remota es 999 (vacía)
            accion = ACCION_UPLOAD
            motivo = "Pendiente de cargar"

        plan.append(PlanRow(
            cu=cu, grupo=grupo,
            cedula=entry.cedula, nombre=entry.nombre,
            instrumento=codigo, instrumento_nombre=nombre_legible,
            nota_local=nota_local_str, nota_remota=remota_str,
            accion=accion, motivo=motivo,
            fuente=entry.fuente,
        ))

    return plan


def _summarize_plan(plan: list[PlanRow]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for p in plan:
        counts[p.accion] = counts.get(p.accion, 0) + 1
    return counts


def _write_plan_csv(plan: list[PlanRow], path: Path) -> None:
    fieldnames = [
        "cu", "grupo", "cedula", "nombre",
        "instrumento", "instrumento_nombre",
        "nota_local", "nota_remota",
        "accion", "motivo", "fuente",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for row in plan:
            w.writerow(dataclasses.asdict(row))


def cmd_plan(args: argparse.Namespace) -> int:
    auth, base_ctx = _load_auth_and_context_from_env(args)
    client = NotasParcialesClient(auth)
    _detect_session_dead(client)

    xlsx_paths = [Path(p) for p in args.xlsx]
    for p in xlsx_paths:
        if not p.exists():
            raise SystemExit(f"xlsx no encontrado: {p}")

    entries, grade_headers = _ingest_xlsx_files(xlsx_paths)
    print(f"Leídos {len(entries)} registros desde {len(xlsx_paths)} archivo(s).")
    print(f"Columnas de nota detectadas: {grade_headers}")

    cu_grupo_map = _parse_cu_grupo_args(args.cu_grupo)
    if not cu_grupo_map:
        raise SystemExit("Se requiere al menos un --cu-grupo (ej. --cu-grupo 42=1 01=2)")
    print(f"CU → grupo: {cu_grupo_map}")

    cu_grupo_pairs = list({(e.cu_xlsx, cu_grupo_map[e.cu_xlsx])
                           for e in entries if e.cu_xlsx in cu_grupo_map})
    print(f"Pares (CU, grupo) a consultar: {cu_grupo_pairs}")

    print("\nConsultando servidor (roster + instrumentos)...")
    roster, instruments_per_grupo = _fetch_server_state(client, base_ctx, cu_grupo_pairs)
    for k, students in roster.items():
        print(f"  CU={k[0]} grupo={k[1]}: {len(students)} estudiantes en notasparciales")

    # Inferir mapeo de instrumentos. Asumimos que TODOS los grupos del mismo modelo
    # comparten los mismos instrumentos; usamos el del primer grupo como referencia.
    if not cu_grupo_pairs:
        raise SystemExit("Sin pares (CU, grupo) válidos: revisá los xlsx y --cu-grupo.")
    ref_key = cu_grupo_pairs[0]
    server_columns = []
    server_encabezados = []
    instr_resp = client.obtener_instrumentos_modelo(
        dataclasses.replace(base_ctx, cu=ref_key[0], grupo=ref_key[1])
    )
    server_columns = instr_resp.get("Tabla_Modelo", []) or []
    server_encabezados = instr_resp.get("Tabla_Encabezados", []) or []
    explicit_map = _parse_explicit_map(args.map or [])
    instrument_mapping = _build_instrument_mapping(
        grade_headers, server_columns, server_encabezados, explicit_map,
    )
    unmapped = [h for h in grade_headers if h not in instrument_mapping]
    print("\nMapeo de instrumentos (xlsx → notasparciales):")
    for h in grade_headers:
        if h in instrument_mapping:
            code, name = instrument_mapping[h]
            print(f"  {h!r}\n    -> {code} ({name})")
        else:
            print(f"  {h!r}\n    -> SIN MAPEO (no se subirá esta columna)")
    if unmapped:
        print("\nADVERTENCIA: las columnas sin mapeo se reportarán como skip en el plan.")
        print("Para forzar mapeo, pasá --map 'COLUMNA=CODIGO' (ej: --map 'Tarea:Entrega Actividad Proyecto Final (Real)=Proy1')")

    print("\nConstruyendo plan...")
    plan = _build_plan(entries, cu_grupo_map, instrument_mapping, roster)
    counts = _summarize_plan(plan)
    print("\nResumen del plan:")
    for accion in (
        ACCION_UPLOAD,
        ACCION_MARK_NOT_PRESENTED,
        ACCION_WOULD_OVERWRITE,
        ACCION_REVIEW,
        ACCION_SKIP_ALREADY,
        ACCION_SKIP_RETIRADO,
        ACCION_SKIP_NOT_IN_ROSTER,
    ):
        if accion in counts:
            print(f"  {accion:30s} {counts[accion]:>4d}")
    extras = set(counts) - {
        ACCION_UPLOAD, ACCION_MARK_NOT_PRESENTED, ACCION_WOULD_OVERWRITE, ACCION_REVIEW,
        ACCION_SKIP_ALREADY, ACCION_SKIP_RETIRADO, ACCION_SKIP_NOT_IN_ROSTER,
    }
    for accion in sorted(extras):
        print(f"  {accion:30s} {counts[accion]:>4d}")

    out = Path(args.output)
    _write_plan_csv(plan, out)
    print(f"\nPlan escrito en: {out}")
    print("Inspeccionalo en Excel/VS Code antes de correr 'apply'.")
    return 0


def cmd_apply(args: argparse.Namespace) -> int:
    auth, base_ctx = _load_auth_and_context_from_env(args)
    client = NotasParcialesClient(auth)
    _detect_session_dead(client)

    plan_path = Path(args.plan)
    if not plan_path.exists():
        raise SystemExit(f"plan.csv no encontrado: {plan_path}")

    rows: list[dict[str, str]] = []
    with plan_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for r in reader:
            rows.append({k: (v or "").strip() for k, v in r.items()})

    # Filtrar a solo las filas que se ejecutan
    target_actions: set[str] = {ACCION_UPLOAD}
    if not args.no_mark_not_presented:
        target_actions.add(ACCION_MARK_NOT_PRESENTED)
    if args.allow_update:
        target_actions.add(ACCION_WOULD_OVERWRITE)
        if not args.justificacion_codigo:
            raise SystemExit(
                "--allow-update requiere --justificacion-codigo (ej. 2005 = Error de digitación)"
            )

    work = [r for r in rows if r["accion"] in target_actions]
    print(f"Filas en el plan: {len(rows)}")
    print(f"Filas a ejecutar (acciones {sorted(target_actions)}): {len(work)}")
    print(f"dry_run={args.dry_run}")

    # Agrupar por (cu, grupo) para mantener el contexto consistente
    work.sort(key=lambda r: (r["cu"], int(r["grupo"]), r["cedula"], r["instrumento"]))
    ok = 0
    failed: list[tuple[dict[str, str], str]] = []
    results: list[dict[str, str]] = []

    for i, r in enumerate(work, 1):
        ctx = dataclasses.replace(
            base_ctx, cu=r["cu"], grupo=int(r["grupo"]),
        )
        instrumento = r["instrumento"]
        nombre = r["instrumento_nombre"]
        cedula = r["cedula"]
        is_no_presento = (r["accion"] == ACCION_MARK_NOT_PRESENTED)
        nota_arg: float | str | None = None if is_no_presento else r["nota_local"]
        display_nota = "NO PRESENTÓ" if is_no_presento else r["nota_local"]

        prefix = f"[{i}/{len(work)}] CU={r['cu']} G={r['grupo']} {cedula} {instrumento}={display_nota}"
        print(f"\n{prefix}  ({r['nombre']})")
        try:
            _upload_one(
                client, ctx,
                cedula=cedula,
                instrumento=instrumento,
                nota=nota_arg,
                no_presento=is_no_presento,
                instrumento_nombre=nombre,
                justificacion_codigo=args.justificacion_codigo,
                justificacion_texto=args.justificacion_texto,
                allow_update=args.allow_update,
                dry_run=args.dry_run,
            )
            ok += 1
            results.append({**r, "resultado": "ok" if not args.dry_run else "dry_run"})
        except Exception as e:  # noqa: BLE001
            logger.error("FALLO: %s", e)
            failed.append((r, str(e)))
            results.append({**r, "resultado": f"FAIL: {e}"})
        time.sleep(args.delay)

    # Persistir resultados al lado del plan
    out = plan_path.with_name(plan_path.stem + "_resultados.csv")
    if results:
        fieldnames = list(results[0].keys())
        with out.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            for r in results:
                w.writerow(r)
        print(f"\nResultados escritos en: {out}")

    print(f"\n== Resumen ==\nOK:    {ok}\nFAIL:  {len(failed)}")
    return 0 if not failed else 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _add_context_args(p: argparse.ArgumentParser, *, cu_grupo_required: bool = True) -> None:
    g = p.add_argument_group("contexto del curso")
    g.add_argument("--ano", required=True, help="Año académico (ej. 2026)")
    g.add_argument("--pac", required=True, help="PAC / cuatrimestre (ej. 3)")
    g.add_argument("--tipo", default="O", help="Tipo de matrícula (default O)")
    g.add_argument("--escuela", required=True, help="Código de escuela (ej. 03)")
    g.add_argument("--catedra", type=int, required=True, help="ID numérico de cátedra (ej. 253)")
    g.add_argument("--encargado", required=True, help="Username del encargado de cátedra")
    g.add_argument("--tutor", required=True, help="Cédula del tutor")
    g.add_argument("--asignatura", required=True, help="Sigla del curso (ej. 00000)")
    if cu_grupo_required:
        g.add_argument("--cu", required=True, help="Código del centro universitario (ej. 42)")
        g.add_argument("--grupo", type=int, required=True, help="Número de grupo (ej. 1)")
    else:
        g.add_argument("--cu", default="", help="(no requerido en este modo: se infiere por fila)")
        g.add_argument("--grupo", type=int, default=0, help="(no requerido en este modo: se infiere por fila)")
    g.add_argument("--modelo", type=int, required=True, help="Número de modelo de evaluación (ej. 4)")
    g.add_argument("--usuario-cedula", default=None, help="Cédula del usuario funcionario (opc, default desde .env)")
    g.add_argument("--usuario-role", type=int, default=None, help="Rol del usuario (default 14 = Tutor)")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="notasparciales_upload",
        description="Sube notas a UNED Notas Parciales (sistema oficial). Auth por cookies (.env).",
    )
    p.add_argument("-v", "--verbose", action="count", default=0, help="-v info, -vv debug")
    p.add_argument("--dry-run", action="store_true", default=None,
                   help="No envía actualizarNotas (default si no se pasa --commit)")
    p.add_argument("--commit", dest="dry_run", action="store_false",
                   help="Confirma escritura real (desactiva dry-run)")
    p.add_argument("--allow-update", action="store_true",
                   help="Permite sobrescribir notas existentes (requiere --justificacion-codigo)")
    p.add_argument("--justificacion-codigo", type=int, default=0,
                   help="Código de justificación si la nota ya existe (1002, 2005, 2003, 1004, 2001, 2000)")
    p.add_argument("--justificacion-texto", default="",
                   help="Texto libre para la justificación")
    p.add_argument("--delay", type=float, default=0.5,
                   help="Segundos entre requests en modo CSV (default 0.5)")

    sub = p.add_subparsers(dest="mode", required=True)

    p_probe = sub.add_parser("probe", help="Verifica auth y descubre instrumentos del modelo")
    _add_context_args(p_probe)

    p_single = sub.add_parser("single", help="Sube UNA nota (ideal para probar)")
    _add_context_args(p_single)
    p_single.add_argument("--cedula", required=True, help="Cédula del estudiante")
    p_single.add_argument("--instrumento", required=True, help="Código del instrumento (ej. Tar1, Proy1)")
    p_single.add_argument("--nota", required=True, help="Nota (0-10, ej. 8.9)")

    p_csv = sub.add_parser("csv", help="Sube un CSV con muchas notas")
    _add_context_args(p_csv)
    p_csv.add_argument("--upload-csv", required=True, help="Path al CSV (cedula,instrumento,nota[,observacion_codigo,justificacion])")

    p_plan = sub.add_parser(
        "plan",
        help="Lee xlsx exportados de Moodle, consulta el servidor y genera un plan.csv auditable",
    )
    _add_context_args(p_plan, cu_grupo_required=False)
    p_plan.add_argument(
        "--xlsx", action="append", required=True,
        help="Ruta a un xlsx (formato exportación Moodle Calificaciones). Repetible.",
    )
    p_plan.add_argument(
        "--cu-grupo", action="append", required=True,
        help="Mapeo CU=GRUPO. Ej: --cu-grupo 42=1 --cu-grupo 01=2",
    )
    p_plan.add_argument(
        "--map", action="append", default=[],
        help="Mapeo explícito de columna del xlsx a código del instrumento. "
             "Ej: --map 'Tarea:Entrega Actividad Proyecto Final (Real)=Proy1'. Repetible.",
    )
    p_plan.add_argument("--output", default="notas_plan.csv", help="Ruta al CSV de salida (default notas_plan.csv)")

    p_apply = sub.add_parser(
        "apply",
        help="Ejecuta un plan.csv generado por 'plan' (con --commit; por defecto dry-run)",
    )
    _add_context_args(p_apply, cu_grupo_required=False)
    p_apply.add_argument("--plan", required=True, help="Ruta al plan.csv generado por el comando 'plan'")
    p_apply.add_argument(
        "--no-mark-not-presented", action="store_true",
        help="No ejecutar las filas con accion=mark_not_presented (por defecto SÍ se ejecutan)",
    )

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    level = logging.WARNING
    if args.verbose == 1:
        level = logging.INFO
    elif args.verbose >= 2:
        level = logging.DEBUG
    logging.basicConfig(level=level, format="%(levelname)s %(message)s")

    if args.dry_run is None:
        args.dry_run = True  # default: no escribe

    if args.mode == "probe":
        return cmd_probe(args)
    if args.mode == "single":
        return cmd_single(args)
    if args.mode == "csv":
        return cmd_upload_csv(args)
    if args.mode == "plan":
        return cmd_plan(args)
    if args.mode == "apply":
        return cmd_apply(args)
    raise SystemExit(f"Modo desconocido: {args.mode}")


if __name__ == "__main__":
    sys.exit(main())

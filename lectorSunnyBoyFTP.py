#!/usr/bin/env python3
"""Lee los archivos de lecturas generados por los inversores Sunny Boy que
suben a un servidor FTP y los guarda en las bases de datos MySQL de la web.

Los inversores se distinguen en los archivos por su número de serie y tienen
que estar creados en la base de datos; se enlazan mediante el campo idBD
definido en configSunnyBoy.xml.

Cada ciclo del bucle principal:
  1. Se conecta al FTP y recorre el directorio configurado de forma recursiva.
  2. Descarga cada archivo zip, extrae los XML que contiene y guarda las
     lecturas en las tablas mensuales "dataloggerAAAAMM.<dato>" de MySQL y,
     para PAC y KDY, también en la tabla "temporal" de la web.
  3. Si el archivo se procesa bien se borra del FTP; si algo falla se mueve a
     la carpeta "errores" del FTP para poder revisarlo en vez de perder datos.
  4. Espera "pausa" segundos y vuelve a empezar.

Todas las rutas (configuración, log y sqlite) se resuelven respecto al
directorio donde está este archivo, así que se puede lanzar desde cualquier
directorio de trabajo (servicio, tarea programada, etc.).

Las contraseñas se pueden sacar del config.cfg y meter en variables de
entorno: SUNNYBOY_MYSQL_PASSWORD y SUNNYBOY_FTP_PASSWORD.
"""

from __future__ import annotations

import configparser
import contextlib
import ftplib
import logging
import os
import re
import sqlite3 as lite
import sys
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from io import BytesIO
from logging.handlers import RotatingFileHandler
from pathlib import Path
from xml.etree import ElementTree as ET

import mysql.connector
import pytz

# ---------------------------------------------------------------------------
# Rutas y constantes
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
RUTA_CONFIG = BASE_DIR / "config.cfg"
RUTA_CONFIG_INVERTERS = BASE_DIR / "configSunnyBoy.xml"
RUTA_KDY = BASE_DIR / "KDYs.sqlite"
RUTA_LOG = BASE_DIR / "fichero_log.log"

FORMATO_FECHA = "%Y-%m-%dT%H:%M:%S"
CARPETA_ERRORES_FTP = "errores"
INTENTOS_BORRADO_FTP = 5
TAMANO_MAX_DESCARGA = 64 * 1024 * 1024  # 64 MiB por archivo del FTP
IDENTIFICADOR_SEGURO = re.compile(r"^[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)?$")

logger = logging.getLogger("lectorSunnyBoy")

# Tabla de MySQL que corresponde a cada dato que puede venir en los XML.
# Actúa también como lista blanca de tablas/columnas válidas.
DATOS = {
    "Pac": "PAC",
    "E-Total": "KDY",
    "Iac-Ist": "IL1",
    "Uac": "UL1",
    "Ipv": "IDC",
    "Upv-Ist": "UDC",
    "GridMs.TotW": "PAC",
    "Metering.TotWhOut": "KDY",
    "GridMs.A.phsA": "IL1",
    "GridMs.A.phsB": "IL2",
    "GridMs.A.phsC": "IL3",
    "GridMs.PhV.phsA": "UL1",
    "GridMs.PhV.phsB": "UL2",
    "GridMs.PhV.phsC": "UL3",
    "Measurement.GridMs.TotW": "PAC",
    "Measurement.Metering.TotWhOut": "KDY",
    "Measurement.GridMs.A.phsA": "IL1",
    "Measurement.GridMs.A.phsB": "IL2",
    "Measurement.GridMs.A.phsC": "IL3",
    "Measurement.GridMs.PhV.phsA": "UL1",
    "Measurement.GridMs.PhV.phsB": "UL2",
    "Measurement.GridMs.PhV.phsC": "UL3",
    "Operation.Health": "Health",
    "Fehler": "Fehler",
    "Status": "Status",
    "Error": "Error",
}

CAMPOS_INVERTER = ("Planta", "Numero", "Linea", "Serie", "idBD")
NIVELES_LOG = ("DEBUG", "INFO", "WARNING", "ERROR")

# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------


class ConfigError(Exception):
    """Error al leer los archivos de configuración."""


@dataclass(frozen=True)
class ConfigBD:
    servidor: str
    puerto: int
    base_datos: str
    usuario: str
    password: str


@dataclass(frozen=True)
class ConfigFTP:
    servidor: str
    puerto: int
    usuario: str
    password: str
    directorio: str
    usar_tls: bool
    timeout: int


@dataclass(frozen=True)
class Configuracion:
    bd: ConfigBD
    ftp: ConfigFTP
    pausa: int
    zona_horaria: str
    nivel_log: str


def _obligatorio(lector: configparser.ConfigParser, seccion: str, clave: str) -> str:
    """Devuelve el valor de una opción obligatoria, comprobando que exista."""
    if not lector.has_option(seccion, clave):
        raise ConfigError(f"Falta la opción [{seccion}] {clave} en {RUTA_CONFIG}")
    valor = lector.get(seccion, clave).strip()
    if not valor:
        raise ConfigError(f"La opción [{seccion}] {clave} de {RUTA_CONFIG} está vacía")
    return valor


def cargar_configuracion() -> Configuracion:
    """Lee config.cfg y valida todas las opciones."""
    lector = configparser.ConfigParser()
    if not lector.read(RUTA_CONFIG, encoding="utf-8"):
        raise ConfigError(f"No existe o no se puede leer {RUTA_CONFIG}")

    bd = ConfigBD(
        servidor=_obligatorio(lector, "BDremota", "server"),
        puerto=lector.getint("BDremota", "port", fallback=3306),
        base_datos=_obligatorio(lector, "BDremota", "database"),
        usuario=_obligatorio(lector, "BDremota", "username"),
        # Las contraseñas se pueden definir con variables de entorno
        password=os.environ.get("SUNNYBOY_MYSQL_PASSWORD") or _obligatorio(lector, "BDremota", "password"),
    )
    ftp = ConfigFTP(
        servidor=_obligatorio(lector, "FTP", "server"),
        puerto=lector.getint("FTP", "port", fallback=21),
        usuario=_obligatorio(lector, "FTP", "username"),
        password=os.environ.get("SUNNYBOY_FTP_PASSWORD") or _obligatorio(lector, "FTP", "password"),
        directorio=_obligatorio(lector, "FTP", "directorio"),
        usar_tls=lector.getboolean("FTP", "tls", fallback=False),
        timeout=lector.getint("FTP", "timeout", fallback=30),
    )

    pausa = lector.getint("General", "pausa", fallback=300)
    if pausa <= 0:
        raise ConfigError(f"[General] pausa debe ser mayor que cero en {RUTA_CONFIG}")

    zona = lector.get("General", "timezone", fallback="Europe/Madrid").strip()
    try:
        pytz.timezone(zona)
    except pytz.UnknownTimeZoneError:
        raise ConfigError(f"[General] timezone desconocida: {zona}") from None

    nivel = lector.get("General", "nivel_log", fallback="INFO").upper()
    if nivel not in NIVELES_LOG:
        raise ConfigError(f"[General] nivel_log debe ser uno de {NIVELES_LOG}")

    return Configuracion(bd=bd, ftp=ftp, pausa=pausa, zona_horaria=zona, nivel_log=nivel)


def configurar_logging(nivel: str) -> None:
    """Configura el log: fichero rotativo + consola."""
    logger.setLevel(nivel)
    formato = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    manejadores = (
        RotatingFileHandler(RUTA_LOG, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    )
    for manejador in manejadores:
        manejador.setFormatter(formato)
        logger.addHandler(manejador)


# ---------------------------------------------------------------------------
# Lecturas e inversores
# ---------------------------------------------------------------------------


class Lectura:
    """Una lectura individual sacada de un XML.

    La clave de los XML tiene la forma "planta:serie:dato" (formato antiguo,
    tipo 0) o "algo:algo:serie:dato" (formatos nuevos, tipo 1 y 2).
    """

    def __init__(self, clave: str, media: str, marca: str, tipo: int):
        partes = clave.split(":")
        if tipo == 0:
            if len(partes) < 3:
                raise ValueError(f"la clave {clave!r} no tiene el formato esperado")
            self.inverter, self.dato = partes[1], partes[2]
        else:
            if len(partes) < 4:
                raise ValueError(f"la clave {clave!r} no tiene el formato esperado (tipo {tipo})")
            self.inverter, self.dato = partes[2], partes[3]
        self.valor = media
        self.hora = marca
        logger.debug("inverter=%s dato=%s valor=%s hora=%s", self.inverter, self.dato, self.valor, self.hora)


@dataclass(frozen=True)
class Inverter:
    """Datos de un inversor tal y como están en configSunnyBoy.xml."""

    planta: str
    numero: str
    linea: str
    serie: str
    idBD: str

    def guarda_lectura(self, lectura: Lectura, almacen: AlmacenMySQL, kdys: AlmacenKDY) -> None:
        """Guarda la lectura en la tabla MySQL que le corresponda."""
        tabla = DATOS[lectura.dato]

        # El KDY (energía del día) hay que calcularlo desde el E-Total
        if tabla == "KDY":
            valor = kdys.calculo(lectura)
            if valor is None:
                logger.warning("No puedo calcular el KDY de %s a las %s", lectura.inverter, lectura.hora)
                return
        else:
            valor = lectura.valor

        # Las lecturas instantáneas van también a la tabla temporal de la web
        if tabla in ("PAC", "KDY"):
            self.guarda_instantaneo(almacen, tabla, valor)

        # Alarmas y estados: de momento solo se dejan en el log
        if tabla in ("Fehler", "Health", "Status", "Error"):
            logger.info(
                "%s=%s del inverter %s en la linea %s de la planta %s en %s",
                tabla, valor, self.numero, self.linea, self.planta, lectura.hora,
            )
            return

        try:
            bd = nombre_bd_mensual(lectura.hora)
        except ValueError:
            logger.warning("Fecha con formato incorrecto (%s), descarto la lectura", lectura.hora)
            return
        almacen.insertar_lectura(
            bd, tabla, (self.planta, self.linea, self.numero, valor, lectura.hora, self.idBD)
        )
        logger.info(
            "Guardo %s=%s del inverter %s en la linea %s de la planta %s en %s",
            tabla, valor, self.numero, self.linea, self.planta, lectura.hora,
        )

    def guarda_instantaneo(self, almacen: AlmacenMySQL, columna: str, valor) -> None:
        """Actualiza la tabla temporal con el último valor instantáneo."""
        sql = (
            f"UPDATE temporal SET {columna} = %s "
            "WHERE planta = %s AND inverter = %s AND linea = %s"
        )
        try:
            filas = almacen.ejecutar(sql, (valor, self.planta, self.numero, self.linea))
            logger.debug("temporal %s=%s (%d filas actualizadas)", columna, valor, filas)
        except mysql.connector.Error:
            logger.exception("No pude actualizar la tabla temporal con %s=%s", columna, valor)


def cargar_inverters() -> dict[str, Inverter]:
    """Lee configSunnyBoy.xml y devuelve los inversores indexados por serie."""
    try:
        raiz = ET.parse(RUTA_CONFIG_INVERTERS).getroot()
    except (OSError, ET.ParseError) as error:
        raise ConfigError(f"No puedo leer {RUTA_CONFIG_INVERTERS}: {error}") from error

    inverters: dict[str, Inverter] = {}
    for nodo in raiz.findall("Inverter"):
        campos = {campo: (nodo.findtext(campo) or "").strip() for campo in CAMPOS_INVERTER}
        if not all(campos.values()):
            logger.warning("Inverter incompleto en %s, lo salto: %s", RUTA_CONFIG_INVERTERS.name, campos)
            continue
        inverter = Inverter(
            planta=campos["Planta"],
            numero=campos["Numero"],
            linea=campos["Linea"],
            serie=campos["Serie"],
            idBD=campos["idBD"],
        )
        if inverter.serie in inverters:
            logger.warning("La serie %s está repetida en %s", inverter.serie, RUTA_CONFIG_INVERTERS.name)
        inverters[inverter.serie] = inverter

    if not inverters:
        raise ConfigError(f"No hay inversores definidos en {RUTA_CONFIG_INVERTERS}")
    logger.info("Cargados %d inversores de %s", len(inverters), RUTA_CONFIG_INVERTERS.name)
    return inverters


# ---------------------------------------------------------------------------
# Almacenes de datos
# ---------------------------------------------------------------------------


class AlmacenKDY:
    """Guarda en sqlite la energía total inicial de cada día e inverter.

    Con ella se calcula el KDY (energía producida en el día) restando la
    energía total actual al valor guardado al empezar el día.
    """

    def __init__(self, ruta: Path):
        self._con = lite.connect(str(ruta))
        self._con.row_factory = lite.Row
        self._con.execute("PRAGMA busy_timeout = 5000")
        with self._con:
            self._con.execute(
                "CREATE TABLE IF NOT EXISTS KDY ("
                "fecha TEXT NOT NULL, serie TEXT NOT NULL, energia_total NUMERIC NOT NULL)"
            )
            try:
                self._con.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_kdy_serie_fecha ON KDY (serie, fecha)"
                )
            except lite.DatabaseError as error:
                logger.warning("No pude crear el índice único de KDYs.sqlite: %s", error)

    def calculo(self, lectura: Lectura) -> float | None:
        """Devuelve el KDY del día para esa lectura (None si no se puede calcular)."""
        try:
            energia = float(lectura.valor)
        except (TypeError, ValueError):
            logger.warning("E-Total no numérico (%r) para la serie %s", lectura.valor, lectura.inverter)
            return None

        fecha = fecha_de(lectura.hora)
        fila = self._con.execute(
            "SELECT energia_total FROM KDY WHERE serie = ? AND fecha = ?",
            (lectura.inverter, fecha),
        ).fetchone()
        if fila is not None:
            return energia - fila["energia_total"]

        # Primer valor del día: se guarda como base y el KDY de ahora es 0
        with self._con:
            self._con.execute(
                "INSERT OR IGNORE INTO KDY (fecha, serie, energia_total) VALUES (?, ?, ?)",
                (fecha, lectura.inverter, energia),
            )
        return 0.0

    def cerrar(self) -> None:
        with contextlib.suppress(Exception):
            self._con.close()


class AlmacenMySQL:
    """Conexión con la BD MySQL de la web, reutilizada entre lecturas.

    Si la conexión se pierde, se reintenta automáticamente en la operación
    siguiente. Un error de MySQL se propaga al llamador para que el archivo
    del FTP se aparte a la carpeta de errores en vez de perderse.
    """

    def __init__(self, config: ConfigBD):
        self._config = config
        self._conexion = None

    def insertar_lectura(self, bd: str, tabla: str, valores: tuple) -> int:
        """Inserta una lectura en la tabla mensual dataloggerAAAAMM.<tabla>."""
        identificador = f"{bd}.{tabla}"
        if not IDENTIFICADOR_SEGURO.match(identificador):
            raise ValueError(f"Identificador de tabla no válido: {identificador}")
        sql = (
            f"INSERT INTO {identificador} "
            "(planta, linea, inverter, valor, fecha, idinverter) "
            "VALUES (%s, %s, %s, %s, %s, %s)"
        )
        return self.ejecutar(sql, valores)

    def ejecutar(self, sql: str, parametros: tuple) -> int:
        """Ejecuta una sentencia con parámetros y hace commit."""
        conexion = self._conectar()
        cursor = conexion.cursor()
        try:
            cursor.execute(sql, parametros)
            conexion.commit()
            return cursor.rowcount
        except mysql.connector.Error:
            # Fuerzo la reconexión en el siguiente intento
            self.cerrar()
            raise
        finally:
            with contextlib.suppress(Exception):
                cursor.close()

    def _conectar(self):
        if self._conexion is not None:
            try:
                self._conexion.ping(reconnect=True, attempts=2, delay=1)
                return self._conexion
            except Exception:
                self.cerrar()
        self._conexion = mysql.connector.connect(
            host=self._config.servidor,
            port=self._config.puerto,
            database=self._config.base_datos,
            user=self._config.usuario,
            password=self._config.password,
            connection_timeout=15,
        )
        return self._conexion

    def cerrar(self) -> None:
        if self._conexion is not None:
            with contextlib.suppress(Exception):
                self._conexion.close()
        self._conexion = None


# ---------------------------------------------------------------------------
# Tratamiento de los XML
# ---------------------------------------------------------------------------


def fecha_de(hora: str) -> str:
    """Devuelve la parte de fecha (YYYY-MM-DD) de una marca temporal."""
    return hora.split("T")[0]


def nombre_bd_mensual(hora: str) -> str:
    """Nombre de la BD mensual (dataloggerAAAAMM) para una marca temporal."""
    fecha = datetime.strptime(hora, FORMATO_FECHA)
    return f"datalogger{fecha.year}{fecha.month:02d}"


def ajustar_zona_horaria(fecha_str: str, zona_horaria: str) -> datetime:
    """Convierte una marca en UTC a la zona horaria indicada."""
    fecha = pytz.utc.localize(datetime.strptime(fecha_str, FORMATO_FECHA))
    return fecha.astimezone(pytz.timezone(zona_horaria))


def normaliza_marca(marca: str | None, tipo: int, offset_utc: str | None, zona_horaria: str) -> str | None:
    """Devuelve la marca temporal en FORMATO_FECHA, o None si no es usable.

    tipo 1 (DST): la marca viene en UTC y hay que convertirla a la zona
    horaria configurada. tipo 2 (UtcOffset): la marca viene en UTC y el propio
    XML indica el desfase en segundos.
    """
    if not marca:
        return None
    if tipo == 1:
        try:
            return ajustar_zona_horaria(marca, zona_horaria).strftime(FORMATO_FECHA)
        except ValueError:
            logger.warning("Marca temporal con formato desconocido: %s", marca)
            return None
    if tipo == 2 and offset_utc:
        try:
            offset = int(float(offset_utc))
            fecha = datetime.strptime(marca, FORMATO_FECHA) + timedelta(seconds=offset)
            return fecha.strftime(FORMATO_FECHA)
        except ValueError:
            logger.warning("UtcOffset (%r) o marca (%s) no válidos", offset_utc, marca)
            return None
    return marca


class Procesador:
    """Procesa los zips descargados del FTP: extrae los XML y guarda las lecturas."""

    def __init__(self, zona_horaria: str, inverters: dict[str, Inverter], almacen: AlmacenMySQL, kdys: AlmacenKDY):
        self.zona_horaria = zona_horaria
        self.inverters = inverters
        self.almacen = almacen
        self.kdys = kdys

    def trata_zip(self, fichero_zip: BytesIO) -> None:
        """Lee un zip y trata los XML que contiene.

        Los zips de Sunny Boy traen entradas "Mean.*" que son a su vez zips
        con un XML dentro, y también XML sueltos con nombre numérico.
        """
        with zipfile.ZipFile(fichero_zip) as principal:
            for nombre in principal.namelist():
                if nombre.endswith("/"):
                    continue  # entradas de directorio dentro del zip
                base = nombre.rsplit("/", 1)[-1]
                if "Mean." in nombre:
                    self._trata_zip_medio(principal, nombre)
                elif base.split(".")[0].isnumeric() or base.split("-")[0].isnumeric():
                    with principal.open(nombre) as xml:
                        self.lee_xml(xml)
                else:
                    logger.debug("Ignoro la entrada %s del zip", nombre)

    def _trata_zip_medio(self, principal: zipfile.ZipFile, nombre: str) -> None:
        """Abre un zip "Mean.*" interno y trata el XML que contiene."""
        with principal.open(nombre) as intermedio:
            with zipfile.ZipFile(intermedio) as interno:
                contenidos = interno.namelist()
                if not contenidos:
                    logger.warning("El zip interno %s está vacío", nombre)
                    return
                with interno.open(contenidos[0]) as xml:
                    self.lee_xml(xml)

    def lee_xml(self, fichero) -> None:
        """Lee las lecturas de un XML y las guarda en la BD que corresponda."""
        arbol = ET.parse(fichero)  # si está mal formado, se propagará y el archivo se apartará
        raiz = arbol.getroot()
        for registro in raiz:
            # El tipo se resetea en cada registro: cada uno se describe a sí mismo
            clave = media = marca = offset_utc = None
            tipo = 0
            for campo in registro:
                texto = (campo.text or "").strip()
                if campo.tag == "Key":
                    if texto and "::" not in texto:
                        clave = texto
                elif campo.tag == "Mean":
                    if texto:
                        media = texto
                elif campo.tag == "DST":
                    tipo = 1
                elif campo.tag == "UtcOffset":
                    tipo = 2
                    offset_utc = texto
                elif campo.tag in ("TimeStamp", "Timestamp") and texto:
                    marca = texto

            marca = normaliza_marca(marca, tipo, offset_utc, self.zona_horaria)
            if not clave or media is None or not marca or clave.startswith("Sunny"):
                logger.debug("Registro incompleto o ajeno, lo salto: %r", clave)
                continue

            try:
                lectura = Lectura(clave, media, marca, tipo)
            except ValueError as error:
                logger.warning("%s", error)
                continue
            self._procesa_lectura(lectura)

    def _procesa_lectura(self, lectura: Lectura) -> None:
        if lectura.dato not in DATOS:
            logger.debug("El dato %s no tiene tabla asignada, lo salto", lectura.dato)
            return
        inverter = self.inverters.get(lectura.inverter)
        if inverter is None:
            logger.warning("La serie %s no está definida en %s", lectura.inverter, RUTA_CONFIG_INVERTERS.name)
            return
        inverter.guarda_lectura(lectura, self.almacen, self.kdys)


# ---------------------------------------------------------------------------
# FTP
# ---------------------------------------------------------------------------


class DescargaDemasiadoGrande(Exception):
    """Un archivo del FTP supera el tamaño máximo permitido."""


def conecta_ftp(config: ConfigFTP) -> ftplib.FTP:
    """Conecta al FTP (opcionalmente con TLS) y se coloca en el directorio de trabajo."""
    ftp = ftplib.FTP_TLS() if config.usar_tls else ftplib.FTP()
    ftp.connect(config.servidor, config.puerto, timeout=config.timeout)
    ftp.login(config.usuario, config.password)
    if config.usar_tls:
        ftp.prot_p()  # cifra también el canal de datos
    ftp.cwd(config.directorio)
    return ftp


def listar_entradas(ftp: ftplib.FTP) -> list[tuple[str, str]]:
    """Devuelve una lista de (nombre, tipo) del directorio actual.

    Usa MLSD cuando el servidor lo soporta; si no, prueba a entrar en cada
    entrada para averiguar si es un directorio.
    """
    try:
        return [
            (nombre, datos.get("type", ""))
            for nombre, datos in ftp.mlsd()
            if nombre not in (".", "..")
        ]
    except Exception as error:
        logger.debug("MLSD no disponible (%s); averiguo los directorios a mano", error)
        entradas = []
        for nombre in ftp.nlst():
            if nombre in (".", ".."):
                continue
            entradas.append((nombre, "dir" if es_directorio(ftp, nombre) else "file"))
        return entradas


def es_directorio(ftp: ftplib.FTP, nombre: str) -> bool:
    """Comprueba si una entrada del FTP es un directorio, sin perder la ruta actual."""
    try:
        ruta_actual = ftp.pwd()
        ftp.cwd(nombre)
        ftp.cwd(ruta_actual)
        return True
    except Exception:
        return False


def descarga_archivo(ftp: ftplib.FTP, nombre: str) -> BytesIO:
    """Descarga un archivo del FTP a memoria, con tope de tamaño."""
    datos = BytesIO()
    total = 0

    def acumula(trozo):
        nonlocal total
        total += len(trozo)
        if total > TAMANO_MAX_DESCARGA:
            raise DescargaDemasiadoGrande(f"{nombre} supera {TAMANO_MAX_DESCARGA} bytes")
        datos.write(trozo)

    ftp.retrbinary("RETR " + nombre, acumula)
    datos.seek(0)
    return datos


def procesar_archivo(ftp: ftplib.FTP, nombre: str, procesador: Procesador) -> None:
    """Descarga un archivo del FTP, lo trata y lo borra si todo va bien.

    Si el procesado falla, el archivo se mueve a la carpeta de errores del FTP
    para revisarlo en vez de perder los datos.
    """
    logger.info("Trato el archivo %s/%s", ftp.pwd(), nombre)
    try:
        contenido = descarga_archivo(ftp, nombre)
        procesador.trata_zip(contenido)
    except Exception:
        logger.exception("No pude procesar %s", nombre)
        mover_a_errores(ftp, nombre)
        return
    if borrar_archivo_ftp(ftp, nombre):
        logger.debug("Borrado %s del FTP", nombre)


def borrar_archivo_ftp(ftp: ftplib.FTP, nombre: str, intentos: int = INTENTOS_BORRADO_FTP) -> bool:
    """Borra un archivo del FTP con reintentos limitados."""
    for intento in range(1, intentos + 1):
        try:
            ftp.delete(nombre)
            return True
        except Exception:
            logger.warning("No pude borrar %s (intento %d de %d)", nombre, intento, intentos)
            time.sleep(min(10 * intento, 30))
    logger.error("Renuncio a borrar %s; quedará en el FTP y se reprocesará", nombre)
    return False


def mover_a_errores(ftp: ftplib.FTP, nombre: str) -> None:
    """Mueve un archivo problemático a la carpeta de errores del FTP."""
    destino = f"{CARPETA_ERRORES_FTP}/{nombre}"
    try:
        with contextlib.suppress(Exception):
            ftp.mkd(CARPETA_ERRORES_FTP)  # puede ya existir
        ftp.rename(nombre, destino)
        logger.info("Movido %s a %s para revisión manual", nombre, destino)
    except Exception:
        logger.exception("No pude mover %s a la carpeta de errores; queda en el FTP", nombre)


def procesar_directorio_actual(ftp: ftplib.FTP, procesador: Procesador) -> None:
    """Recorre el directorio actual del FTP de forma recursiva y lo procesa todo."""
    visitados = {ftp.pwd()}
    for entrada, tipo in listar_entradas(ftp):
        if entrada == CARPETA_ERRORES_FTP:
            continue
        _procesa_entrada(ftp, entrada, tipo, procesador, visitados)


def _procesa_entrada(ftp: ftplib.FTP, entrada: str, tipo: str, procesador: Procesador, visitados: set) -> None:
    if tipo == "file" or (tipo != "dir" and not es_directorio(ftp, entrada)):
        procesar_archivo(ftp, entrada, procesador)
        return
    _recorre_directorio(ftp, entrada, procesador, visitados)


def _recorre_directorio(ftp: ftplib.FTP, nombre: str, procesador: Procesador, visitados: set) -> None:
    """Entra en un subdirectorio, procesa su contenido y vuelve a subir."""
    ftp.cwd(nombre)
    ruta = ftp.pwd()
    if ruta in visitados:
        logger.warning("Ya he visitado %s (posible bucle), no vuelvo a entrar", ruta)
    else:
        visitados.add(ruta)
        logger.debug("Entro en el directorio %s", ruta)
        for entrada, tipo in listar_entradas(ftp):
            if entrada == CARPETA_ERRORES_FTP:
                continue
            _procesa_entrada(ftp, entrada, tipo, procesador, visitados)
    ftp.cwd("..")


# ---------------------------------------------------------------------------
# Bucle principal
# ---------------------------------------------------------------------------


def un_ciclo(config: Configuracion, procesador: Procesador) -> None:
    """Un ciclo completo: conecta al FTP, procesa lo que haya y desconecta."""
    ftp = conecta_ftp(config.ftp)
    try:
        procesar_directorio_actual(ftp, procesador)
    finally:
        try:
            ftp.quit()
        except Exception:
            with contextlib.suppress(Exception):
                ftp.close()


def main() -> int:
    try:
        config = cargar_configuracion()
    except ConfigError as error:
        print(f"Error de configuración: {error}", file=sys.stderr)
        return 2
    configurar_logging(config.nivel_log)
    logger.info(
        "Arranco el lector (pausa=%ds, zona=%s, ftp=%s)",
        config.pausa, config.zona_horaria, config.ftp.servidor,
    )

    try:
        inverters = cargar_inverters()
    except ConfigError as error:
        logger.error("%s", error)
        return 2

    almacen = AlmacenMySQL(config.bd)
    kdys = AlmacenKDY(RUTA_KDY)
    procesador = Procesador(config.zona_horaria, inverters, almacen, kdys)
    try:
        while True:
            try:
                un_ciclo(config, procesador)
            except KeyboardInterrupt:
                logger.info("Parada por el usuario")
                break
            except Exception:
                # Un fallo puntual (red, MySQL, FTP) no debe tumbar el proceso
                logger.exception("Error en el ciclo; reintento tras la pausa")
            logger.info("Espero %d segundos", config.pausa)
            time.sleep(config.pausa)
    finally:
        almacen.cerrar()
        kdys.cerrar()
    return 0


if __name__ == "__main__":
    sys.exit(main())

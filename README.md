# Lector de logs Sunny Boy

Programa que lee los archivos de lecturas generados por los inversores Sunny
Boy (subidos a un servidor FTP) y los guarda en las bases de datos MySQL del
servidor web.

Cada inversor se distingue en los archivos por su **número de serie** y tiene
que estar creado en la base de datos; se enlazan mediante el campo `idBD`
definido en `configSunnyBoy.xml`.

## Qué hace en cada ciclo

1. Se conecta al FTP y recorre el directorio configurado de forma recursiva.
2. Descarga cada archivo zip, extrae los XML que contiene (los `Mean.*` que son
   zips internos y los XML sueltos con nombre numérico) y guarda las lecturas:
   - En las tablas mensuales `dataloggerAAAAMM.<dato>` de MySQL.
   - Para `PAC` y `KDY`, también en la tabla `temporal` de la web (último
     valor instantáneo).
   - El `KDY` (energía del día) se calcula restando el `E-Total` actual al
     primer `E-Total` del día, que se guarda en `KDYs.sqlite`.
3. Si el archivo se procesa bien se **borra del FTP**; si algo falla se mueve
   a la carpeta **`errores/` del FTP** para poder revisarlo en vez de perder
   los datos.
4. Espera `pausa` segundos y vuelve a empezar. Un fallo puntual de red, FTP o
   MySQL no detiene el proceso: se registra en el log y se reintenta en el
   ciclo siguiente.

## Instalación

```
python -m pip install -r requirements.txt
```

Requiere Python 3.8 o superior.

## Configuración

### `config.cfg`

| Sección | Opción | Descripción |
|---|---|---|
| `BDremota` | `server`, `port`, `database`, `username`, `password` | Conexión MySQL de la web. |
| `FTP` | `server`, `port`, `username`, `password` | Conexión al FTP. |
| `FTP` | `directorio` | Carpeta del FTP donde están los logs. |
| `FTP` | `tls` | `yes` para usar FTPS (conexión y descargas cifradas) si el servidor lo soporta. Por defecto `no`. |
| `FTP` | `timeout` | Segundos antes de dar una operación FTP por fallida. Por defecto `30`. |
| `General` | `pausa` | Segundos de espera entre ciclos. Por defecto `300`. |
| `General` | `timezone` | Zona horaria a la que convertir las marcas que llegan en UTC. Por defecto `Europe/Madrid`. |
| `General` | `nivel_log` | `DEBUG`, `INFO`, `WARNING` o `ERROR`. Por defecto `INFO`. |

Las contraseñas se pueden sacar del archivo y definir en variables de
entorno: `SUNNYBOY_MYSQL_PASSWORD` y `SUNNYBOY_FTP_PASSWORD` (tienen
prioridad sobre `config.cfg`).

### `configSunnyBoy.xml`

Un bloque `<Inverter>` por inverso con los campos `Planta`, `Numero`,
`Linea`, `Serie` y `idBD`. La `Serie` debe coincidir con el número de serie
que aparece en las claves de los XML de los logs.


## Archivos que genera

- `fichero_log.log`: log rotativo (máximo 5 MB × 3 copias).
- `KDYs.sqlite`: energía total inicial de cada día por inverter, para el
  cálculo del KDY. Tiene un índice único `(serie, fecha)`.

Todas las rutas se resuelven respecto al directorio donde está el programa,
así que se puede lanzar desde cualquier directorio de trabajo (servicio de
Windows, systemd, tarea programada…).

## Reprocesar archivos de `errores/`

Si un archivo falló y se movió a la carpeta `errores/` del FTP, se puede
volver a procesar moviéndolo de nuevo a la carpeta que vigilaba el programa.
**Ojo:** las lecturas que sí llegaron a guardarse de ese archivo se
duplicarán, porque las tablas mensuales no tienen clave única. Revisa el log
para ver hasta dónde llegó antes de devolverlo.

## Cambios respecto a la versión anterior

- El proceso ya no muere con un fallo puntual de red/FTP/MySQL; reintenta en
  el ciclo siguiente.
- Los archivos que no se pueden procesar se apartan en `errores/` en vez de
  borrarse.
- Una única conexión MySQL reutilizada (antes se abría una por cada lectura).
- Queries SQL parametrizadas en vez de concatenación de cadenas.
- `KDYs.sqlite`, el log y el XML de configuración se buscan junto al
  programa, no en el directorio de trabajo.
- El borrado en el FTP tiene un número de intentos limitado (antes podía
  recursionar sin fin).
- El tipo de registro (antiguo, `DST`, `UtcOffset`) se detecta por registro,
  y los archivos `UtcOffset` convierten su marca con el offset que indican.
- Log rotativo con niveles en lugar de `print`.
- Soporte de FTPS y del `port` del FTP en la configuración.

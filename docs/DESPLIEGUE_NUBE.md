# Despliegue en la nube (AWS Academy)

El sistema se desplegó en máquinas virtuales EC2 de AWS Academy (Learner Lab) con los scripts de `deploy/aws/` y se validó el 2026-10-14 19:41:04 con `dfsha-cloud validate` desde un cliente externo, con tráfico real sobre Internet. Cada nodo corre en su propio contenedor Docker con la misma imagen.

| Rol | Nodo | Zona de disponibilidad | Dirección pública |
|---|---|---|---|
| client | cl1 | us-east-1f | `ec2-98-92-16-167.compute-1.amazonaws.com` |
| control | c1 | us-east-1f | `ec2-98-92-33-233.compute-1.amazonaws.com` |
| control | c2 | us-east-1d | `ec2-3-89-191-250.compute-1.amazonaws.com` |
| control | c3 | us-east-1a | `ec2-3-234-249-169.compute-1.amazonaws.com` |
| data | dn1 | us-east-1f | `ec2-44-223-43-56.compute-1.amazonaws.com` |
| data | dn2 | us-east-1d | `ec2-98-81-1-135.compute-1.amazonaws.com` |
| data | dn3 | us-east-1a | `ec2-98-92-235-71.compute-1.amazonaws.com` |
| data | dn4 | us-east-1b | `ec2-34-226-248-193.compute-1.amazonaws.com` |

Los DataNodes abarcan 4 zonas de disponibilidad (us-east-1a, us-east-1b, us-east-1d, us-east-1f); la colocación de réplicas usa cada AZ como *zona*, de modo que la pérdida de una AZ completa no destruye datos.

## Validación en la nube

| Paso | Resultado | Detalle | s |
|---|---|---|---|
| 1. Conectividad y líder único | **PASS** | 3 ControlNodes responden por HTTPS verificado contra la CA; líder = c2 (término 1) | 0.93 |
| 2. Login y topología | **PASS** | 4 DataNodes vivos en 4 zonas ['us-east-1a', 'us-east-1b', 'us-east-1d', 'us-east-1f'] | 0.19 |
| 3. Controles de seguridad de red | **PASS** | http_plano: rechazado; tls_sin_ca: rechazado (CERTIFICATE_VERIFY_FAILED); api_sin_token: HTTP 503; bloque_sin_token: HTTP 401; puerto_interno: no alcanzable desde Internet (security group correcto) | 5.31 |
| 4. put/get con verificación SHA-256 | **PASS** | 64 MiB en 17 bloques; subida 8 MiB/s, bajada 2 MiB/s; SHA-256 idéntico | 33.99 |
| 5. Mapa de bloques (réplicas y zonas) | **PASS** | 17 bloques × 3 réplicas, repartidos en 4 DataNodes ['dn1', 'dn2', 'dn3', 'dn4'] | 0.45 |
| 6. Cifrado en reposo en el disco | **PASS** | dn1: 12 bloques en disco, cabecera DFSB (AES-GCM) y 0 coincidencias del marcador en claro que se subió | 1.71 |
| 7. Failover del líder | **PASS** | líder c2 detenido → escritura aceptada otra vez en 5.6s por el nuevo líder c1; datos íntegros; c2 se reincorporó como seguidor en 3s | 37.44 |
| 8. Caída de un DataNode | **PASS** | dn1 detenido → lectura inmediata correcta (30.6s); subreplicación detectada a 31s; 3 réplicas vivas por bloque restablecidas a 31s | 37.43 |
| 9. fsck final | **PASS** | 18 bloques, todos sanos | 0.08 |
| 10. Limpieza | **PASS** | archivos de prueba eliminados | 0.44 |

**Veredicto: todos los pasos superados.**

* **Transferencia sobre Internet** (64,0 MiB en 17 bloques): subida 7,8 MiB/s y bajada 2,5 MiB/s desde el cliente externo, con SHA-256 idéntico.
* **Failover del líder en la nube**: se detuvo el contenedor del líder `c2` y el servicio volvió a aceptar escrituras en **5,6 s** con el nuevo líder `c1`; los datos previos siguieron íntegros y el nodo se reincorporó como seguidor en 3,1 s.
* **Caída de un DataNode**: tras detener `dn1`, la lectura inmediata fue correcta (30,6 s); la subreplicación se detectó a los 31,0 s y se restablecieron 3 réplicas vivas por bloque a los **31,2 s** (con `dn_dead_after`=10 s y `repl_grace`=15 s, los valores por defecto de los contenedores).

## Rendimiento con nodos en máquinas distintas

Medido desde una VM cliente del mismo VPC (4 DataNodes en total, 2026-10-15 00:53:05); se varía el número de DataNodes *activos* excluyendo al resto con `decommissioning` (los contenedores no se tocan).

| Replicación | DataNodes activos | Subida MiB/s | Bajada MiB/s |
|---|---|---|---|
| ×1 | 1 | 81,5 ± 0,7 | 104 ± 0,6 |
| ×1 | 2 | 114 ± 20,0 | 114 ± 3,9 |
| ×1 | 3 | 135 ± 7,8 | 119 ± 1,9 |
| ×1 | 4 | 139 ± 36,3 | 114 ± 4,2 |
| ×3 | 3 | 55,4 ± 0,6 | 117 ± 2,4 |
| ×3 | 4 | 60,3 ± 1,7 | 115 ± 2,2 |

![Escalado en la nube frente a una sola máquina](report/figs/cloud_escalado.png)

Con replicación ×1, pasar de 1 a 4 DataNodes cambia la subida de 81,5 a 139 MiB/s (1,7×). A diferencia del experimento de una sola máquina, aquí cada DataNode tiene su propia CPU, red y disco, por lo que el escalado refleja el comportamiento distribuido real.

La **bajada** apenas cambia con el número de DataNodes (entre 104 y 119 MiB/s): a partir de ahí el límite deja de estar en el cluster y pasa a la única VM cliente (CPU para TLS y AES-GCM, o su red). No se aisló cuál de las dos; para medirlo habría que lanzar varios clientes en paralelo. Con replicación ×3 la subida es menor porque cada bloque se escribe tres veces (tubería de replicación).

## Efecto del paralelismo y del tamaño de archivo

| Configuración | Subida MiB/s | Bajada MiB/s |
|---|---|---|
| workers=8,size=256MiB | 59,6 | 114 |

## Operaciones de metadatos en la nube

| Clientes | stat linealizable ops/s (p99 ms) | mkdir Raft ops/s (p99 ms) |
|---|---|---|
| 1 | 518 (3,3) | 78,4 (19,5) |
| 8 | 900 (13,8) | 211 (54,4) |
| 32 | 526 (298) | 328 (172) |

## Evidencia gráfica

![Consola EC2 las 8 instancias en ejecucion en distintas zonas](deploy/aws/evidence/capturas/01-Consola-EC2-las-8-instancias-en-ejecucion-en-distintas-zonas.png)

![Security group reglas de entrada](deploy/aws/evidence/capturas/02-Security-group-reglas-de-entrada.png)

![dfsha nodes lider seguidores y DataNodes por zona](deploy/aws/evidence/capturas/03-dfsha-nodes-lider-seguidores-y-DataNodes-por-zona.png)

![dfsha blocks replicas en distintas zonas](deploy/aws/evidence/capturas/04-dfsha-blocks-replicas-en-distintas-zonas.png)

![Cifrado en reposo cabecera DFSB en el disco de un DataNode](deploy/aws/evidence/capturas/05-Cifrado-en-reposo-cabecera-DFSB-en-el-disco-de-un-DataNode.png)

![Failover del lider Raft tras matar al lider](deploy/aws/evidence/capturas/06-Failover-del-lider-Raft-tras-matar-al-lider.png)

![Re replicacion automatica tras matar un DataNode](deploy/aws/evidence/capturas/07-Re-replicacion-automatica-tras-matar-un-DataNode.png)

![Validacion automatica en AWS 10 pasos superados](deploy/aws/evidence/capturas/08-Validacion-automatica-en-AWS-10-pasos-superados.png)

![Benchmark de escalado medido desde una VM del VPC](deploy/aws/evidence/capturas/09-Benchmark-de-escalado-medido-desde-una-VM-del-VPC.png)

![Permisos ACL usuario samuel denegado y luego permitido](deploy/aws/evidence/capturas/10-Permisos-ACL-usuario-samuel-denegado-y-luego-permitido.png)

![Acceso desde otro computador Windows al cluster en AWS](deploy/aws/evidence/capturas/11-Acceso-desde-otro-computador-Windows-al-cluster-en-AWS.png)


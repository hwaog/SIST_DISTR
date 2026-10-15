# Evidencia de validación en la nube — 2026-10-14 19:41:04

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

**Veredicto:** TODOS LOS PASOS SUPERADOS

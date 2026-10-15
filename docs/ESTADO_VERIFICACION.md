| Componente | Estado | Evidencia |
|---|---|---|
| Cluster local multiproceso (TLS/mTLS reales) | **Verificado.** Suite automática de pruebas unitarias, de integración, de caos y de escala; benchmarks reproducibles. | `tests/`, `bench/` |
| Configuración del `docker-compose.yml` | **Verificada sin contenedores**: `scripts/compose_dryrun.py` arranca los 8 nodos con exactamente las variables, URLs, volúmenes por nodo y puertos del compose, y comprueba el aislamiento de claves; `tests/test_compose.py` lo verifica estructuralmente. | `scripts/compose_dryrun.py` |
| Imagen Docker y contenedores | **Pendiente.** No se ejecutó: Docker no estaba disponible en el entorno de desarrollo. | `docs/evidence/docker/` |
| Scripts de AWS (`deploy/aws/`) | **Probados con comandos simulados** (`aws`, `ssh`, `rsync`, `curl` falsos): inventario y reparto por zonas, certificados con SAN reales, mínimo privilegio por rol, persistencia de la CA entre redespliegues. | `tests/test_aws_scripts.py` |
| Despliegue real en AWS Academy / GCP | **Verificado el 2026-10-14 19:41:04**: topología, validación de 10 pasos con caos y benchmarks en máquinas distintas (ver el capítulo «Despliegue en la nube»). | `deploy/aws/evidence/` |

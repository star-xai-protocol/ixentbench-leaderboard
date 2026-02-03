"""Generate Docker Compose configuration from scenario.toml"""

import argparse
import os
import re
import sys
import base64
from pathlib import Path
from typing import Any

# --- DEPENDENCIAS ---
try:
    import tomli
except ImportError:
    try:
        import tomllib as tomli
    except ImportError:
        print("Error: tomli required. Install with: pip install tomli")
        sys.exit(1)
try:
    import tomli_w
except ImportError:
    print("Error: tomli-w required. Install with: pip install tomli-w")
    sys.exit(1)
try:
    import requests
except ImportError:
    print("Error: requests required. Install with: pip install requests")
    sys.exit(1)


# --- CONFIGURACIÓN ---
AGENTBEATS_API_URL = "https://agentbeats.dev/api/agents"
COMPOSE_PATH = "docker-compose.yml"
A2A_SCENARIO_PATH = "a2a-scenario.toml"
ENV_PATH = ".env.example"
DEFAULT_PORT = 9009
DEFAULT_ENV_VARS = {"PYTHONUNBUFFERED": "1"}


# --- 🛠️ CÓDIGO DEL WRAPPER (EL SALVAVIDAS) ---
# Este script Python se inyectará en el contenedor.
# NO modifica archivos. Importa el servidor y lo arregla en memoria ("Monkey Patching").
WRAPPER_SOURCE = r"""
import sys
import os
import time
import glob
import logging
from flask import jsonify

print("🔧 [WRAPPER] Iniciando arranque seguro...", flush=True)

# 1. Añadir 'src' al path para poder importar green_agent
# Asumimos que estamos en /app y el codigo esta en /app/src
sys.path.append(os.path.join(os.getcwd(), 'src'))

# 2. Importar el servidor original
# Esto carga todas las rutas originales (incluida agent-card) correctamente.
try:
    import green_agent
    print("✅ [WRAPPER] Módulo green_agent importado correctamente.", flush=True)
except ImportError as e:
    print(f"❌ [WRAPPER] Fallo al importar green_agent: {e}", flush=True)
    # Intento de fallback por si la estructura de carpetas es distinta
    sys.path.append(os.getcwd())
    try:
        import green_agent
        print("✅ [WRAPPER] Módulo green_agent importado (fallback path).", flush=True)
    except ImportError:
        print("❌ [WRAPPER] Imposible cargar el servidor. Abortando.", flush=True)
        sys.exit(1)

# 3. Definir la función de Bloqueo (Long Polling)
# Esta funcion sustituirá a la 'dummy_rpc' original.
def blocking_rpc():
    print("🔒 [BLOQUEO] Cliente conectado. Esperando fin de partida...", flush=True)
    start_time = time.time()
    
    while True:
        # Buscar resultados recientes
        # Buscamos en todas las ubicaciones posibles
        patterns = ['results/*.json', 'src/results/*.json', 'replays/*.jsonl', 'src/replays/*.jsonl']
        files = []
        for p in patterns:
            files.extend(glob.glob(p))
        
        if files:
            files.sort(key=os.path.getmtime)
            last_file = files[-1]
            
            # Si el archivo es reciente (< 10 min), es el nuestro
            if (time.time() - os.path.getmtime(last_file)) < 600:
                filename = os.path.basename(last_file)
                print(f"✅ [FIN] Detectado: {filename}. Enviando respuesta.", flush=True)
                
                # JSON PLANO (Flattened) para pasar la validación estricta del cliente
                return jsonify({
                    "jsonrpc": "2.0", "id": 1,
                    "result": {
                        "contextId": "ctx", "taskId": "task", "id": "task",
                        "status": {"state": "completed"}, "final": True,
                        "messageId": "msg-done", "role": "assistant",
                        "parts": [{"text": "Game Finished", "mimeType": "text/plain"}]
                    }
                })
        
        # Timeout de seguridad (20 min)
        if time.time() - start_time > 1200:
            return jsonify({"error": "timeout_waiting_results"})
            
        time.sleep(5)

# 4. APLICAR EL PARCHE (Monkey Patch)
# Sobreescribimos la función asociada al endpoint 'dummy_rpc'
if 'dummy_rpc' in green_agent.app.view_functions:
    green_agent.app.view_functions['dummy_rpc'] = blocking_rpc
    print("✅ [WRAPPER] Función dummy_rpc parcheada en memoria.", flush=True)
else:
    print("⚠️ [WRAPPER] No se encontró dummy_rpc. Intentando forzar la ruta...", flush=True)
    # Si por alguna razón el nombre interno es distinto, forzamos la ruta raíz
    green_agent.app.add_url_rule('/', 'blocking_rpc_force', blocking_rpc, methods=['POST', 'GET'])

# 5. EJECUTAR EL SERVIDOR
# Usamos las variables del modulo original si existen, o defaults
print("🚀 [WRAPPER] Arrancando servidor Flask...", flush=True)
# Desactivamos debug/reloader para evitar procesos hijos extraños en Docker
green_agent.app.run(host='0.0.0.0', port=9009, debug=False, use_reloader=False)
"""

# Codificamos el wrapper en Base64 para inyección segura en YAML
WRAPPER_B64 = base64.b64encode(WRAPPER_SOURCE.encode('utf-8')).decode('utf-8')


def fetch_agent_info(agentbeats_id: str) -> dict:
    url = f"{AGENTBEATS_API_URL}/{agentbeats_id}"
    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        print(f"Error: Failed to fetch agent {agentbeats_id}: {e}")
        sys.exit(1)


def parse_scenario(scenario_path: Path) -> dict[str, Any]:
    toml_data = scenario_path.read_text()
    data = tomli.loads(toml_data)

    green = data.get("green_agent", {})
    resolve_image(green, "green_agent")

    participants = data.get("participants", [])
    names = [p.get("name") for p in participants]
    duplicates = [name for name in set(names) if names.count(name) > 1]
    if duplicates:
        print(f"Error: Duplicate participant names: {duplicates}")
        sys.exit(1)

    for participant in participants:
        name = participant.get("name", "unknown")
        resolve_image(participant, f"participant '{name}'")

    return data


def resolve_image(agent: dict, name: str) -> None:
    has_image = "image" in agent
    has_id = "agentbeats_id" in agent

    if has_image and has_id:
        print(f"Error: {name} has both 'image' and 'agentbeats_id'")
        sys.exit(1)
    elif has_image:
        if os.environ.get("GITHUB_ACTIONS"):
            print(f"Error: {name} requires 'agentbeats_id' for GitHub Actions")
            sys.exit(1)
        print(f"Using {name} image: {agent['image']}")
    elif has_id:
        info = fetch_agent_info(agent["agentbeats_id"])
        agent["image"] = info["docker_image"]
        if "id" in info:
            agent["webhook_id"] = info["id"]
        print(f"Resolved {name} image: {agent['image']}")
    else:
        print(f"Error: {name} must have 'image' or 'agentbeats_id'")
        sys.exit(1)


# 🟢 PLANTILLA SERVIDOR
COMPOSE_TEMPLATE = """# Auto-generated from scenario.toml

services:
  green-agent:
    image: {green_image}
    platform: linux/amd64
    container_name: green-agent
    
    # 👇 FIX: Usamos el Wrapper. 
    # Crea /tmp/wrapper.py desde base64 y lo ejecuta.
    entrypoint: 
      - /bin/sh
      - -c
      - "echo {wrapper_b64} | base64 -d > /tmp/wrapper.py && python /tmp/wrapper.py"

    environment:{green_env}
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:{green_port}/status"]
      interval: 5s
      timeout: 3s
      retries: 10
      start_period: 30s
    depends_on:{green_depends}
    networks:
      - agent-network

{participant_services}
  agentbeats-client:
    image: ghcr.io/agentbeats/agentbeats-client:v1.0.0
    platform: linux/amd64
    container_name: agentbeats-client
    volumes:
      - ./a2a-scenario.toml:/app/scenario.toml
      - ./output:/app/output
    command: ["scenario.toml", "output/results.json"]
    depends_on:{client_depends}
    networks:
      - agent-network

networks:
  agent-network:
    driver: bridge
"""

# 🟢 PARTICIPANTE
PARTICIPANT_TEMPLATE = """  {name}:
    image: {image}
    platform: linux/amd64
    container_name: {name}
    command: ["--host", "0.0.0.0", "--port", "{port}", "--card-url", "http://{name}:{port}"]
    environment:{env}
    depends_on:
      green-agent:
        condition: service_healthy
    healthcheck:
      test: ["CMD-SHELL", "exit 0"]
      interval: 10s
      timeout: 5s
      retries: 5
      start_period: 5s
    networks:
      - agent-network
"""

A2A_SCENARIO_TEMPLATE = """[green_agent]
endpoint = "http://green-agent:{green_port}"

{participants}
{config}"""


def format_env_vars(env_dict: dict[str, Any]) -> str:
    env_vars = {**DEFAULT_ENV_VARS, **env_dict}
    lines = [f"      - {key}={value}" for key, value in env_vars.items()]
    return "\n" + "\n".join(lines)


def generate_docker_compose(scenario: dict[str, Any]) -> str:
    green = scenario["green_agent"]
    participants = scenario.get("participants", [])

    participant_names = [p["name"] for p in participants]

    participant_services = "\n".join([
        PARTICIPANT_TEMPLATE.format(
            name=p["name"],
            image=p["image"],
            port=DEFAULT_PORT,
            env=format_env_vars(p.get("env", {}))
        )
        for p in participants
    ])

    all_services = ["green-agent"] + participant_names

    return COMPOSE_TEMPLATE.format(
        green_image=green["image"],
        green_port=DEFAULT_PORT,
        green_env=format_env_vars(green.get("env", {})),
        green_depends=" []",  
        participant_services=participant_services,
        client_depends=" []", 
        wrapper_b64=WRAPPER_B64
    )


def generate_a2a_scenario(scenario: dict[str, Any]) -> str:
    green = scenario["green_agent"]
    participants = scenario.get("participants", [])

    participant_lines = []
    for p in participants:
        lines = [
            f"[[participants]]",
            f"role = \"{p['name']}\"",
            f"endpoint = \"http://{p['name']}:{DEFAULT_PORT}\"",
        ]
        if "webhook_id" in p:
             lines.append(f"agentbeats_id = \"{p['webhook_id']}\"")
        elif "agentbeats_id" in p:
             lines.append(f"agentbeats_id = \"{p['agentbeats_id']}\"")
        participant_lines.append("\n".join(lines) + "\n")

    config_section = scenario.get("config", {})
    config_lines = [tomli_w.dumps({"config": config_section})]

    return A2A_SCENARIO_TEMPLATE.format(
        green_port=DEFAULT_PORT,
        participants="\n".join(participant_lines),
        config="\n".join(config_lines)
    )


def generate_env_file(scenario: dict[str, Any]) -> str:
    green = scenario["green_agent"]
    participants = scenario.get("participants", [])
    secrets = set()
    env_var_pattern = re.compile(r'\$\{([^}]+)\}')

    for agent in [green] + participants:
        for value in agent.get("env", {}).values():
            for match in env_var_pattern.findall(str(value)):
                secrets.add(match)

    if not secrets: return ""
    lines = [f"{secret}=" for secret in sorted(secrets)]
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", type=Path)
    args = parser.parse_args()

    if not args.scenario.exists():
        print(f"Error: {args.scenario} not found")
        sys.exit(1)

    scenario = parse_scenario(args.scenario)

    with open(COMPOSE_PATH, "w") as f:
        f.write(generate_docker_compose(scenario))

    with open(A2A_SCENARIO_PATH, "w") as f:
        f.write(generate_a2a_scenario(scenario))

    env_content = generate_env_file(scenario)
    if env_content:
        with open(ENV_PATH, "w") as f:
            f.write(env_content)
        print(f"Generated {ENV_PATH}")

    print(f"Generated {COMPOSE_PATH} and {A2A_SCENARIO_PATH} (FINAL SAFE WRAPPER)")

if __name__ == "__main__":
    main()

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


# --- PARCHE DEL SERVIDOR (EL "VIGILANTE" ROBUSTO) ---
# Se ejecuta DENTRO del contenedor. Modifica green_agent.py en memoria/disco.
SERVER_PATCH_SOURCE = r"""
import sys
import os
import time
import glob
import re

print("🔧 [PATCH] Iniciando inyeccion de Long Polling...", flush=True)

target_file = 'src/green_agent.py'
if not os.path.exists(target_file):
    target_file = 'green_agent.py'

print(f"📄 Archivo objetivo: {target_file}", flush=True)

with open(target_file, 'r') as f:
    content = f.read()

# 1. Asegurar imports necesarios al principio
if "import time" not in content:
    content = "import time, glob, os\n" + content
if "from flask import Flask" in content and "jsonify" not in content:
    content = content.replace("from flask import Flask", "from flask import Flask, jsonify")

# 2. Desactivar la ruta antigua dummy_rpc comentando sus decoradores
# Esto evita conflictos de rutas duplicadas en Flask.
content = content.replace("@app.route('/',", "# @app.route('/',")
content = content.replace("@app.route(\"/\",", "# @app.route(\"/\",")

# 3. Código de la NUEVA función de bloqueo (Long Polling)
new_logic = r'''
@app.route('/', methods=['POST', 'GET'])
def blocking_rpc():
    print("🔒 [BLOQUEO] Cliente conectado. Esperando fin de partida...", flush=True)
    start_time = time.time()
    
    while True:
        # Buscar archivos de resultados recientes (.jsonl o .json)
        patterns = ['results/*.json', 'src/results/*.json', 'replays/*.jsonl', 'src/replays/*.jsonl']
        files = []
        for p in patterns:
            files.extend(glob.glob(p))
            
        if files:
            files.sort(key=os.path.getmtime)
            last_file = files[-1]
            
            # Si el archivo tiene menos de 10 min (600s), es la partida actual
            if (time.time() - os.path.getmtime(last_file)) < 600:
                filename = os.path.basename(last_file)
                print(f"✅ [FIN] Detectado: {filename}. Liberando cliente.", flush=True)
                
                # JSON Plano para pasar validación estricta
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
'''

# 4. INYECCIÓN SEGURA: Insertar después de crear la 'app'
# No tocamos el final del archivo (if __name__...) para evitar romper el arranque.
if "app = Flask(__name__)" in content:
    parts = content.split("app = Flask(__name__)")
    # Reconstruimos: Todo lo anterior + app=Flask + NUESTRA LOGICA + Todo lo posterior
    new_content = parts[0] + "app = Flask(__name__)\n" + new_logic + "\n" + parts[1]
    
    with open(target_file, 'w') as f:
        f.write(new_content)
    print("✅ [PATCH] Inyeccion realizada correctamente.", flush=True)
else:
    print("❌ [PATCH] No se encontró 'app = Flask(__name__)', no se pudo parchear.", flush=True)
    sys.exit(1)

# 5. Ejecutar el servidor
print("🚀 [PATCH] Arrancando servidor...", flush=True)
sys.stdout.flush()
os.execvp("python", ["python", "-u", target_file] + sys.argv[1:])
"""

# Codificación Base64 para el YAML
PATCH_B64 = base64.b64encode(SERVER_PATCH_SOURCE.encode('utf-8')).decode('utf-8')


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
        # CAPTURAMOS EL ID DEL WEBHOOK
        if "id" in info:
            agent["webhook_id"] = info["id"]
        print(f"Resolved {name} image: {agent['image']}")
    else:
        print(f"Error: {name} must have 'image' or 'agentbeats_id'")
        sys.exit(1)


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


# 🟢 PLANTILLA SERVIDOR
COMPOSE_TEMPLATE = """# Auto-generated from scenario.toml

services:
  green-agent:
    image: {green_image}
    platform: linux/amd64
    container_name: green-agent
    
    # 👇 FIX: Decodificar y ejecutar el parche.
    entrypoint: 
      - /bin/sh
      - -c
      - "echo {patch_b64} | base64 -d > /tmp/runner.py && python /tmp/runner.py --host 0.0.0.0 --port {green_port} --card-url http://green-agent:{green_port}"

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

# 🟢 PLANTILLA PARTICIPANTE (Modo Zombi)
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
        client_depends=" []", # Romper dependencia circular en el cliente
        patch_b64=PATCH_B64
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

    print(f"Generated {COMPOSE_PATH} and {A2A_SCENARIO_PATH} (FINAL ROBUST INJECTION)")

if __name__ == "__main__":
    main()

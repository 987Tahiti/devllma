import os, sys
sys.path.insert(0, r"C:\Devllma")
os.chdir(r"C:\Devllma")

from db import init, new_session, list_sessions, save_artifact, history, stats, mem_get
from brain import think
from agents import call, AGENTS

WS = r"C:\Devllma\workspace"
os.makedirs(WS, exist_ok=True)

BANNER = """
\033[1;35m
  ____              _     _     __  __ _    
 |  _ \  _____   _| |   | |   |  \/  / \   
 | | | |/ _ \ \ / / |   | |   | |\/| / _ \  
 | |_| |  __/\ V /| |___| |___| |  |/ ___ \ 
 |____/ \___| \_/ |_____|_____|_|  /_/   \_|
\033[0m
\033[36m  Systeme IA Local | Brain + 10 Agents | Ollama | SQLite
  Workspace: C:\\Devllma\\workspace\033[0m
"""

HELP = """
\033[1mCOMMANDES:\033[0m
  ask  <question>   Demande au Brain (orchestrateur intelligent)
  code <tache>      Agent CODER directement
  arch <tache>      Agent ARCHITECT  
  debug <pb>        Agent DEBUGGER
  review            Agent REVIEWER (sur dernier resultat)
  test  <code>      Agent TESTER
  db   <question>   Agent DATABASE
  sec  <code>       Agent SECURITY
  ops  <tache>      Agent DEVOPS
  front <tache>     Agent FRONTEND
  back  <tache>     Agent BACKEND

  agents            Liste des 11 agents
  history           Historique de la session
  sessions          Sessions precedentes
  stats             Statistiques de la base
  save <fichier>    Sauvegarde le dernier resultat dans workspace
  clear             Nouvelle session
  aide / help       Cette aide
  quit              Quitter
"""

DIRECT = {
    "code": "coder", "arch": "architect", "debug": "debugger",
    "test": "tester", "db": "database", "sec": "security",
    "ops": "devops", "front": "frontend", "back": "backend",
}

def main():
    print(BANNER)
    init()
    sid = new_session(f"Session {len(list_sessions())+1}")
    print(f"\033[32m[DB] Session #{sid} demarree - Base: C:\\Devllma\\database\\devllma.db\033[0m\n")
    last = ""

    while True:
        try:
            raw = input("\033[1;35mDevLLMA>\033[0m ").strip()
            if not raw: continue
            parts = raw.split(None, 1)
            cmd = parts[0].lower()
            args = parts[1] if len(parts) > 1 else ""

            if cmd in ("quit","exit","q"):
                s = stats()
                print(f"\n\033[32mSession terminee | {s['messages']} messages | {s['tasks']} taches | {s['artifacts']} artifacts\033[0m")
                break
            elif cmd in ("aide","help","?"):
                print(HELP)
            elif cmd == "agents":
                print("\n\033[1mAgents disponibles:\033[0m")
                for n,a in AGENTS.items():
                    print(f"  \033[36m{n:12}\033[0m {a['desc']}")
            elif cmd == "history":
                h = history(sid, 10)
                for x in h: print(f"\n\033[36m[{x[0].upper()}]\033[0m ({x[1]}): {x[2][:300]}")
            elif cmd == "sessions":
                for s in list_sessions(): print(f"  #{s[0]} {s[1]} ({s[2]})")
            elif cmd == "stats":
                s = stats()
                for k,v in s.items(): print(f"  {k}: {v}")
            elif cmd == "clear":
                sid = new_session(f"Session {len(list_sessions())+1}")
                print(f"\033[32mNouvelle session #{sid}\033[0m")
            elif cmd == "review":
                last = call("reviewer", f"Revois ce code/resultat:\n{last}", stream=True)
            elif cmd == "save":
                name = args or "output.txt"
                path = os.path.join(WS, name)
                with open(path, "w", encoding="utf-8") as f: f.write(last)
                save_artifact(name, last, path=path)
                print(f"\033[32mSauvegarde: {path}\033[0m")
            elif cmd == "model":
                # Vitesses mesurees reellement sur ce poste (bench_models.py), pas des estimations.
                MODELES = {
                    "1.5b":     "qwen2.5-coder:1.5b",
                    "3b":       "qwen2.5-coder:3b",
                    "7b":       "qwen2.5-coder:7b",
                    "14b":      "qwen2.5-coder:14b",
                    "30b":      "qwen3-coder:30b",
                    "devstral": "devstral-small-2:24b",
                    "qwen3":    "qwen3:14b",
                    "r1":       "deepseek-r1:32b",
                }
                VITESSES = {
                    "1.5b":     "Ultra rapide   ~30 tok/s (estime)",
                    "3b":       "Rapide         ~18 tok/s (estime)",
                    "7b":       "Bon            ~7  tok/s (mesure)",
                    "14b":      "Tres bon       ~3  tok/s (estime)",
                    "30b":      "Rapide+precis  ~13 tok/s (MoE, mesure) - RECOMMANDE",
                    "devstral": "Precis mais lent ~2.3 tok/s (dense 24b, mesure)",
                    "qwen3":    "Lent, tres verbeux (mode reflexion)  ~3 tok/s (mesure)",
                    "r1":       "Raisonnement profond, tres lent (dense 32b)",
                }
                if args in MODELES:
                    nouveau = MODELES[args]
                    for a in AGENTS: AGENTS[a]["model"] = nouveau
                    print(f"\033[1;32m[OK] Modele: {nouveau} ({VITESSES[args]})\033[0m")
                else:
                    print("\033[33mUsage: model 1.5b | 3b | 7b | 14b | 30b | devstral | qwen3 | r1\033[0m")
                    for k,v in VITESSES.items():
                        cur = " <-- actif" if AGENTS["coder"]["model"] == MODELES[k] else ""
                        print(f"  model {k:9}  {v}{cur}")
            elif cmd in DIRECT:
                if not args: print(f"Usage: {cmd} <description>"); continue
                last = call(DIRECT[cmd], args, stream=True)
            elif cmd == "ask":
                if not args: print("Usage: ask <question>"); continue
                last = think(sid, args, stream=True)
            else:
                # Par defaut -> Brain
                last = think(sid, raw, stream=True)
        except KeyboardInterrupt:
            print("\n(Ctrl+C - tape 'quit' pour quitter)")
        except Exception as e:
            print(f"\033[31mErreur: {e}\033[0m")

if __name__ == "__main__":
    main()

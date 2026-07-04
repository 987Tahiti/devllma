from agents import call, route, AGENTS, is_research_question, has_dev_keywords
from db import msg, save_task, history, mem_index

def think(sid, user_input, stream=True):
    from agents import GREETINGS
    if user_input.lower().strip() in GREETINGS or len(user_input.strip()) < 5:
        print(f"\n\033[1;36m[BRAIN]\033[0m Pret. Decris ton projet.")
        from db import msg as db_msg
        db_msg(sid, "brain", "assistant", "Pret. Decris ton projet.")
        return "Pret. Decris ton projet."
    if is_research_question(user_input) and not has_dev_keywords(user_input):
        return _research(sid, user_input, stream)
    return _orig_think(sid, user_input, stream)

def _research(sid, user_input, stream=True):
    """Question generale / recherche internet — ne passe pas par le pipeline de dev."""
    from tools import web_search
    print(f"\n\033[1;33m[RESEARCHER] Recherche web...\033[0m")
    hits = web_search(user_input, 5)
    search_ctx = "\n".join(f"- {h.get('title','')}: {h.get('snippet','')} ({h.get('url','')})" for h in hits) \
        if hits else "(aucun resultat)"
    prompt = f"QUESTION: {user_input}\n\nRESULTATS DE RECHERCHE WEB:\n{search_ctx}\n\nReponds a la question."
    result = call("researcher", prompt, stream=stream)
    msg(sid, "researcher", "assistant", result)
    save_task(sid, "researcher", user_input, result)
    mem_index("qa", user_input[:80], f"Q: {user_input}\nR: {result[:600]}")
    return result

def _orig_think(sid, user_input, stream=True):
    """Brain analyse la demande et orchestre les agents"""
    # Historique recent
    hist = history(sid, 4)
    ctx = "\n".join([f"[{h[0].upper()}]: {h[2][:150]}" for h in hist]) if hist else "Debut de session"

    # Brain analyse
    plan_prompt = f"""DEMANDE: {user_input}
CONTEXTE: {ctx}

Analyse en 2 phrases max et indique quel(s) agent(s) utiliser parmi:
ARCHITECT, CODER, DEBUGGER, REVIEWER, TESTER, DEVOPS, DATABASE, FRONTEND, BACKEND, SECURITY

Reponds: AGENT(S): X,Y | PLAN: description courte"""

    print(f"\n\033[1;33m[BRAIN] Analyse...\033[0m")
    plan = call("brain", plan_prompt, stream=False)
    print(f"\033[33m{plan}\033[0m")
    msg(sid, "brain", "assistant", plan)

    # Route vers agents identifies
    agents_needed = route(user_input)

    results = []
    for agent in agents_needed[:2]:
        agent_prompt = f"""TACHE: {user_input}

PLAN DU BRAIN: {plan}

Reponds en tant qu agent {agent.upper()} specialise. Sois precis et complet."""
        result = call(agent, agent_prompt, stream=stream)
        msg(sid, agent, "assistant", result)
        save_task(sid, agent, user_input, result)
        results.append((agent, result))

    # Synthese si plusieurs agents
    if len(results) > 1:
        synth = "\n\n".join([f"[{a.upper()}]: {r[:600]}" for a,r in results])
        synth_prompt = f"Synthese des agents pour: {user_input}\n\n{synth}\n\nResume coherent en 3-5 lignes."
        print(f"\n\033[1;33m[BRAIN] Synthese...\033[0m")
        final = call("brain", synth_prompt, stream=stream)
        msg(sid, "brain", "assistant", final)
        return final

    return results[0][1] if results else ""

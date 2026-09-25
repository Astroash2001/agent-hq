# 🏢 AgentHQ (Dunder Cliffin Edition) — 16-Bit Multi-Agent 2D Workspace Harness

> A production-ready, extensible **Multi-Agent Orchestration Engine** with a live **16-Bit Retro Pixel Art Office (Theme A)** where you can watch specialist AI agents receive tasks, type on CRT terminals, pass documents, execute tools, and synthesize results in real-time.

---

## 🎮 Features at a Glance

* **🪵 16-Bit Cozy Wooden Floor Canvas**: Procedural mahogany planks, red executive boss rug, bubbling water cooler, espresso machine, and CRT desk monitors.
* **👑 Boss Orchestrator (Michael)**: Decomposes high-level user tasks into sequential DAG workflows and routes subtasks to specialists.
* **🤖 Specialist Agent Team**:
  * **🔍 Andy (Researcher)**: RAG, web search, document retrieval (`web_search`, `read_file`).
  * **💻 Jim (Code & Bug Hunter)**: Python AST analysis, test runner, shell execution (`terminal`, `code_analysis`, `write_file`).
  * **🗄️ Kevin (Data Specialist)**: Safe queries, directory file inspection (`list_files`, `read_file`).
  * **🎨 Pam (Synthesis & UI)**: Markdown reports and executive briefings.
* **📟 Amber Phosphor CRT Terminal**: Live real-time stream of agent reasoning steps and tool execution outputs.
* **⛽ Token Fuel Tank & Cost Gauge**: Real-time telemetry monitoring tokens used and estimating API cost in dollars.
* **🚨 Emergency Fire Alarm**: Instant kill switch to interrupt runaway loops and cancel all agents.
* **⚡ Decoupled Event Bus**: Strongly typed Pydantic events broadcasted over bidirectional WebSockets.

---

## 🏗️ Architecture

```mermaid
flowchart TD
    User["User Input / Preset Pill"] --> WS["FastAPI WebSocket (/ws)"]
    WS --> EventBus["Asynchronous In-Memory EventBus"]
    EventBus --> Boss["Boss Orchestrator (Michael)"]

    subgraph SpecialistWorkforce["Specialist Agents (ReAct Loop)"]
        Boss -->|task.assigned (Traveling Orb)| Andy["Andy (Researcher)"]
        Boss -->|task.assigned (Traveling Orb)| Jim["Jim (Code Hunter)"]
        Boss -->|task.assigned (Traveling Orb)| Kevin["Kevin (Data)"]
        Boss -->|task.assigned (Traveling Orb)| Pam["Pam (Synthesis)"]
    end

    subgraph ToolRegistry["Tool Hub (Safe Execution)"]
        Andy --> T1["web_search / read_file"]
        Jim --> T2["terminal / code_analysis"]
        Kevin --> T3["list_files / read_file"]
    end

    SpecialistWorkforce -->|agent.status / tool.called / token.update| EventBus
    EventBus -->|Live Broadcast| Canvas["2D Pixel Canvas (Wooden Floor & Sprites)"]
    EventBus -->|Live Broadcast| HUD["Retro Amber CRT HUD & Token Meter"]
```

---

## 🚀 Quick Start

### 1. Start the Backend Server
```bash
cd "C:\Users\avina\Desktop\agentic AI\agent-hq\backend"
python main.py
```

### 2. Open in Your Browser
Navigate to:
👉 **[http://localhost:8080](http://localhost:8080)**

---

## 🧪 Run Automated Tests
```bash
cd "C:\Users\avina\Desktop\agentic AI\agent-hq\backend"
python -m pytest tests -v
```
All **11 tests** covering Schemas, EventBus, Tool Registry, Base Agent ReAct Loop, Orchestrator Routing, and WebSockets will pass!

---

## 🧠 Interview Guide: How to Explain This Project

| Question | Your Confident Answer |
|---|---|
| *"What is an Agent Harness?"* | *"An LLM by itself is just a text generator. The harness is the outer scaffolding that provides tools, memory, safety guardrails (like step caps and AST validation), and event-driven telemetry."* |
| *"Why use an Event Bus?"* | *"It decouples the LLM execution from the frontend. The agents simply emit domain events (like `task.assigned` or `tool.called`) and the Event Bus broadcasts them over WebSockets without blocking the agent loop."* |
| *"How do you prevent infinite loops?"* | *"We implemented a hard step-capper (`max_agent_steps=8`) inside `BaseAgent.run()` and an emergency kill switch that halts all coroutines immediately."* |

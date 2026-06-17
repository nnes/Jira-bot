# AGENT_SPEC.md - Multi-Model AI Agent System Specification

You are acting in PLAN mode. Please analyze the following architectural requirement to build a Multi-Model AI Agent backend integrated with MS Teams (via Bot Framework Emulator locally) and Jira/Confluence. Outline the system design, file structure, state management, and implementation steps before coding.

### PROJECT OVERVIEW
Build a Multi-Model AI Agent backend for an Zalopay engineering team. The Agent automates the lifecycle of transforming raw, incomplete user descriptions into high-quality, production-ready Jira Tickets according to industry standards.

### API CREDENTIALS & LOCAL ENVIRONMENT
- **LLM Configuration:** Base URL & OpenAI-compatible API Key will be provided via environment variables (`LLM_BASE_URL`, `LLM_API_KEY`). Use standard OpenAI SDK or LangChain adapters supporting custom `base_url`.
- **MS Teams Local Integration:** Since the Azure Service for MS Teams is not registered yet, the system MUST connect via **Bot Framework Emulator** on a personal computer. 
  - Use Microsoft's `botbuilder-core` and Python-based Bot Framework SDK templates.
  - Expose a local endpoint (typically `http://localhost:3978/api/messages`).
  - Configure the bot backend to run locally without requiring MicrosoftAppId and MicrosoftAppPassword for Emulator testing, or handle them via mock values.

### MULTI-MODEL ARCHITECTURE & ROUTING
1. **Core Orchestrator & Chatbot (Model: `minimax/minimax-m2.5`):**
   - Interacts with the Bot Framework Emulator interface.
   - Responsible for Intent Recognition and Slot-Filling.
   - Chat back-and-forth with the user to gather enough context. Maintain conversation state.
2. **Context Enricher & Knowledge Router (Model: `qwen/qwen3-reranker-8b`):**
   - Triggered ONLY when the user provides a Confluence link.
   - Performs semantic search/reranking over Confluence page content (PRD, System Design) to extract and enrich technical contexts.
3. **Fintech Logic & Ticket Generator (Model: `qwen/qwen3-5-27b`):**
   - Processes the unified context (Chat + Confluence data).
   - Infer financial/technical context specialized in Fintech (E-wallet, Payment, Banking, Transactions...).
   - Automatically fill missing information gaps using built-in fintech domain knowledge.
   - Formulate professional, clear, unambiguous engineering specifications.
   - Map data to JSON structure required by `JIRA_TICKET_SPEC.md` and `change-requirement-template.md`.

### STRICT SECURITY GUARDRAILS (CRITICAL)
- **Jira Restrictions:** ONLY allow Read and Create operations (Epic, Story, Task). Absolutely NO destructive operations (DELETE) allowed. Any UPDATE operations must trigger an explicit confirmation loop with the user.
- **Confluence Restrictions:** Strictly READ-ONLY. No creation, modification, or deletion of Confluence pages.
- **Fintech Compliance:** Ensure the final generated ticket text never includes simulated PII (Plain Card Numbers, PINs, Passwords).

### TECHNICAL STACK REQUIREMENTS
- **Language:** Python 3.11+
- **Framework:** LangGraph (preferred for stateful, cyclic multi-agent workflows) and `botbuilder-core` / `aiohttp` for the local Bot Framework server.
- **Integration:** Blueprint/Structure for Bot Framework Emulator, Jira Server REST API client, and Confluence REST API client.

### EXPECTED FILES TO BE PLANNED
1. `config.py`: Environment variables, API Clients (Jira, Confluence, LLM Provider, Bot Settings).
2. `bot.py`: Main Bot logic extending `ActivityHandler` to process incoming/outgoing activities from the Emulator.
3. `state.py`: LangGraph state definitions holding user context, missing slots, confluence data, and final ticket JSON.
4. `agents/`:
   - `teams_agent.py` (minimax-m2.5 logic)
   - `reranker_agent.py` (qwen3-reranker-8b logic)
   - `jira_generator.py` (qwen3-5-27b logic)
5. `tools/`: Jira tool definitions (safe methods only), Confluence reader.
6. `main.py`: Aiohttp App setup, Graph compilation, and server entry point.

---

### YOUR TASK (IN PLAN MODE)
1. **Deconstruct the Local Bot Workflow:** Provide a blueprint of how the local `bot.py` catches requests from Bot Framework Emulator, passes the stream to the LangGraph state machine, and pipes the response back to the Emulator.
2. **Define Prompt Strategies:** Draft system prompts for `minimax-m2.5` (focused on slot-filling and emulator chat UX) and `qwen3-5-27b` (fintech logic).
3. **Propose Local Debugging & Error Handling:** Plan for scenarios where the local emulator disconnects, or the custom LLM gateway returns a timeout.
4. **Present the Architectural Blueprint:** Show the proposed directory structure and mock code signatures for `bot.py` and `main.py` before executing.

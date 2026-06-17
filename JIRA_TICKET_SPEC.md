# Jira Ticket Specification & Automation Rules

## General Configurations
- **Project Key**: Need User provide
<!-- - **Product Domain**: `Bank Solutions` (Default custom field value for all tickets) -->
- Be concise and direct to ensure quick, accurate comprehension.

## Jira Custom Field IDs (jira.zalopay.vn)
| Field Name | Custom Field ID | Required for |
|------------|----------------|--------------|
| Acceptance criteria | `customfield_10300` | — |
| Actual Build Date | `customfield_12201` | — |
| BAU Category | `customfield_13719` | — |
| BAU Effort (Minute) | `customfield_13718` | — |
| Bug Classification | `customfield_12301` | — |
| Bug in Environments | `customfield_13800` | — |
| Deployment Ready | `customfield_14108` | — |
| End date | `customfield_10413` | — |
| Epic Link | `customfield_10101` | — |
| Epic Name | `customfield_10103` | Epic |
| Estimate | `customfield_11900` | — |
| Executive Summary | `customfield_12103` | — |
| Impact | `customfield_12012` | — |
| Involved Services | `customfield_14100` | — |
| Man-day | `customfield_13325` | — |
| Objective (OKR) | `customfield_12106` | — |
| OKR Workstreams | `customfield_12303` | — |
| Parent Link | `customfield_11416` | — |
| Platform | `customfield_12204` | — |
| PM | `customfield_10405` | — |
| PMO Comment | `customfield_10700` | — |
| PO PIC | `customfield_10303` | — |
| Probability | `customfield_12010` | — |
| Product Domain | `customfield_13710` | — |
| Program | `customfield_12200` | — |
| Program | `customfield_13001` | — |
| Program Baseline Sprint | `customfield_13004` | — |
| Program Commitments | `customfield_13005` | — |
| Program Period | `customfield_13003` | — |
| Program RAG | `customfield_13006` | — |
| Program RAG Comments | `customfield_13007` | — |
| Program Team | `customfield_13002` | — |
| QC PIC | `customfield_10418` | — |
| Regression | `customfield_12004` | — |
| Risk Contingency Action | `customfield_12016` | — |
| Risk Mitigation Action | `customfield_12015` | — |
| Sandbox Date | `customfield_11702` | — |
| Severity | `customfield_11102` | — |
| Sprint | `customfield_10100` | — |
| Squad/Portfolio Related | `customfield_12105` | — |
| Start date | `customfield_10412` | — |
| Stg Date | `customfield_11703` | — |
| Story Points | `customfield_10801` | — |
| Sub Domain | `customfield_13711` | — |
| Target end | `customfield_11418` | — |
| Target start | `customfield_11417` | — |
| Task Category | `customfield_12404` | Task |
| Tech PIC | `customfield_10304` | — |
| Test Complete Date | `customfield_13704` | — |
| Test Start Date | `customfield_13703` | — |
| ZLP Environment | `customfield_12100` | — |

## 1. Epic Creation Rules
- **Issue Type**: `Epic`
- **Epic Name & Summary**: AI must auto-generate based on user input.
  - **Required Format**: `[System/Service Name] <Action or Capability description...>`
  - *Example*: `[BC] BIDV OAO integration`
- **Description Field**: Agent MUST ask: *"Bạn có muốn dùng Change Requirement Template cho Description không?"*
  - **Yes** → follow **Change Requirement Template** section below.
  - **No** → AI free-form enriches user input (context + key details), no fixed structure required.
- **Priority**: Default to `P3 (Medium)` if not provided.
- **Assignee**: Default to `Unassigned` (Empty) if not provided.
- **Sprint**: Default to `Next Sprint` if not provided.

## 2. Story Creation Rules
- **Issue Type**: `Story`
- **Epic Link**:
  - **Mandatory**: Agent MUST ask the user for the Epic ID/Link code.
  - **Validation**: Agent must call Jira Server API to verify the Epic exists before linking.
- **Summary**: AI must auto-generate using the same format: `[System/Service Name] <Action or Capability description...>`
- **Description Field**: Agent MUST ask: *"Bạn có muốn dùng Change Requirement Template cho Description không?"*
  - **Yes** → follow **Change Requirement Template** section below.
  - **No** → AI free-form enriches user input based on the specific IT task.
- **Assignee**: Default to `Unassigned` (Empty) if not provided.
- **Sprint**: Default to `Next Sprint` if not provided.
- **Story Points**: Default to `0` if not provided by the user.

## 3. Task Creation Rules
- **Issue Type**: `Task`
- **Task Category** (`customfield_12404`): **Required**. Agent MUST ask user to choose one:
  - `Tech Initiative`
  - `Tech Debt`
  - `Deployment`
  - `Integration Test`
  - `BAU`
- **Epic Link**:
  - **Mandatory**: Agent MUST ask the user for the Epic ID/Link code.
  - **Validation**: Agent must call Jira Server API to verify the Epic exists before linking.
- **Summary**: AI must auto-generate using the same format: `[System/Service Name] <Action or Capability description...>`
- **Description**: AI free-form enriches user input. **Does NOT use Change Requirement Template.**
- **Assignee**: Default to `Unassigned` (Empty) if not provided.
- **Sprint**: Default to `Next Sprint` if not provided.
- **Story Points**: Default to `0` if not provided by the user.

## Change Requirement Template
> Applied to **Epic** and **Story** Description only, when user explicitly opts in.

The 3-section structure for Description Field:
  1. **Context**: Background and technical ecosystem. Briefly describe why this change is needed, including the current problem, business/product goal, or pain point this change aims to solve.
  2. **Requirement**: Detailed functional updates. AI ask user to choose 1 from 3 section: Product, Technical, Configuration. Then AI must auto-generate content based on user input.
     - **Product**: Describe the expected product behavior from the user's perspective, including affected users, entry point, UI changes, user actions, system response, states, edge cases, and tracking if applicable. Can add link confluence for PRD.
     - **Technical**: Describe the technical changes required on the back-end/front-end side, including API, database, related services, configuration, dependencies, or diagrams needed for the team to understand the scope before grooming.
     - **Configuration**: Describe the configuration to be changed, where it is located, how to apply it, and whether the change requires a service restart.
  3. **Acceptance Criteria**: Format **Checklist** (`- [ ] <criterion>`). Define the conditions that must be met for the ticket to be considered done, including expected output, deployment environment, tracking/logging/monitoring, rollout plan, or migration if applicable.
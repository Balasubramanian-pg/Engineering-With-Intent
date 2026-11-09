
<img width="2000" height="1125" alt="image" src="https://github.com/user-attachments/assets/5662453b-6774-48b8-81e5-92b33a610bcc" />

# The Databricks Genie Code Handbook 1.0

> **INSTRUCTIONS · AGENT SKILLS · GOVERNANCE · PRODUCTION PLAYBOOK**  
> A practical engineering handbook on building, configuring and governing Databricks Genie Code for more predictable, reliable workflows.  

## About This Handbook

This handbook brings together my research, experiments and implementation work around Databricks Genie Code. It is intended for platform engineers, data architects, analytics engineers and anyone looking to introduce more structure and predictability into their agentic workflows.

Rather than treating Genie Code as a standalone coding assistant, I approach it as something that needs the right instructions, context, supporting agents and validation mechanisms around it.

Each section follows a consistent structure:

- **Core Concept:** One specific engineering problem or architectural decision, explained with a clear point of view.
- **Architecture and Visuals:** Diagrams, matrices, graphs and technical breakdowns to make the underlying concepts easier to understand.
- **How It Works:** An explanation of the mechanics, implementation details and potential failure scenarios.
- **Production Considerations:** Practical recommendations, things to watch out for and implementation guidelines.
- **Try It Yourself:** Links to companion scripts, SQL examples and hands-on exercises that you can deploy and test in Databricks.

## How to Navigate the Handbook

The handbook contains 43 landscape pages, divided into four parts and eight chapters.

- **Page 01:** Front cover.
- **Page 02:** Interactive table of contents organised around six stages of the engineering lifecycle: Scope, Instruct, Extend, Ground, Eval and Govern. Each chapter and page number is clickable.
- **Pages 03–40:** Eight chapters covering architecture, context, skills, governance, Unity Catalog, security, evaluation and production operations.
- **Page 41:** Glossary of technical and architectural terminology.
- **Page 42:** Master reference index containing official documentation and external research links.
- **Page 43:** Five practical habits for keeping Genie Code production-ready.

## What the Handbook Covers

### Part I: Architecture and Context

<img width="1539" height="868" alt="image" src="https://github.com/user-attachments/assets/38338ac8-535f-4a6d-9dec-df67b23487f6" />

#### Chapter 1: The Precedence Ladder (Pages 03–07)

Understanding how Genie Code processes instructions, resolves conflicting context and determines which rules take precedence.

- **The Engine Mental Model (p. 04):** How workspace files, notebook cells and catalog metadata contribute to the assistant's working context.
- **The Instruction Hierarchy (p. 05):** A breakdown of the five instruction tiers and how they influence model behaviour.
- **The Silent Override Trap (p. 06):** Understanding what happens when instructions conflict, why these conflicts can be difficult to identify and how to detect them.
- **The Lean Instruction Budget (p. 07):** My observations on keeping instructions concise, including the distinction between a practical 2,000-character working budget and the platform's 20,000-character ceiling.

#### Chapter 2: Context Windows and Hygiene (Pages 08–12)

Why context management matters and how to avoid carrying unnecessary conversational history into engineering tasks.

- **The Three-Turn Context Cliff (p. 09):** Exploring how longer conversations can introduce context degradation and lead to issues such as dropped CTEs and incorrect column handling.
- **Stateless Prompt Design (p. 10):** Building self-contained notebook prompts with explicit catalog, schema and entity references.
- **The Explicit Reset Pattern (p. 11):** Exploring when session resets using `@reset` can help restore a clean working context.
- **Session Execution Modes (p. 12):** Comparing execution approaches and understanding when side-panel interactions are preferable to heavy inline workflows.

### Part II: Skills and Repositories

#### Chapter 3: Modular Agent Skills (Pages 13–17)

Breaking larger engineering workflows into smaller, reusable skills instead of overloading global instructions.

- **The Anatomy of a Skill (p. 14):** Understanding the `SKILL.md` structure, frontmatter, trigger descriptions and execution instructions.
- **Installing Skills with aitools (p. 15):** Managing skill installation, versioning and distribution across team workspaces using the Databricks CLI.
- **Dynamic Trigger Collision (p. 16):** Understanding how overlapping skill descriptions can cause unintended triggers and how to test for them.
- **Lakeflow Pipelines Skill Matrix (p. 17):** A reusable skill template for generating production-grade streaming pipelines.

#### Chapter 4: Repository-Level Governance (Pages 18–21)

Using repository-level instructions to maintain consistent engineering practices without making global workspace instructions unnecessarily restrictive.

- **Directory Tree Walking (p. 19):** Exploring how local `AGENTS.md` files can provide additional instructions as engineers work across project directories.
- **SQL Dialect and Casing Standards (p. 20):** Applying consistent SQL formatting, uppercase keywords and snake_case naming conventions.
- **PySpark versus SQL Guidance (p. 21):** Providing context that helps the assistant choose between PySpark and Spark SQL based on the workload and execution environment.

### Part III: Unity Catalog Grounding

#### Chapter 5: Relational Grounding (Pages 22–26)

Reducing incorrect joins and ambiguous table selection by providing Genie with better metadata and relational context.

- **Informational Constraints (p. 23):** Using Primary Key and Foreign Key metadata in Unity Catalog to help establish the correct relationships between tables.
- **Cardinality and Fan Traps (p. 24):** Understanding how one-to-many relationships can introduce duplicate aggregations and incorrect metrics, and how to document these relationships.
- **Partition Pruning Annotations (p. 25):** Documenting partition columns and relevant table metadata to help guide more efficient query generation.
- **Table and Column Synonyms (p. 26):** Mapping technical and legacy ERP identifiers such as `kunnr` and `vbeln` to business-friendly terminology through Unity Catalog metadata.

#### Chapter 6: Security and Access Control (Pages 27–31)

Making sure that agent-generated queries operate within the right access boundaries and that the underlying data remains protected.

- **Row Filters and Column Masks (p. 28):** Understanding how Genie operates within the caller's permissions and how Unity Catalog security policies apply to generated queries.
- **System Table Audit Trails (p. 29):** Using `system.query.history` to investigate query execution, user activity and operational failures, subject to available permissions and audit coverage.
- **Warehouse Throttling Limits (p. 30):** Managing compute costs through warehouse auto-stop settings, query timeouts and appropriate execution limits.
- **Curated Gold View Isolation (p. 31):** Restricting end-user discovery to certified business views instead of exposing raw Bronze and Silver tables unnecessarily.

### Part IV: Operations and CI/CD

#### Chapter 7: Evaluation Benchmarks and CI (Pages 32–36)

Moving beyond manually checking whether an agent-generated answer looks correct and introducing repeatable evaluation into the development workflow.

- **The Golden Question Suite (p. 33):** Building a representative set of 25 business questions with verified SQL queries and expected results.
- **Three-Dimensional Assertions (p. 34):** Evaluating generated queries across execution success, SQL structural similarity and result-level parity.
- **Automated Regression Gates (p. 35):** Introducing CI/CD checks that flag instruction changes when benchmark performance drops below an agreed threshold.
- **Declarative Asset Bundles (p. 36):** Managing workspace configurations, instructions and supporting resources as version-controlled deployments using Databricks Asset Bundles.

#### Chapter 8: Production Field Playbook (Pages 37–40)

Bringing the individual concepts together into a practical operational workflow.

- **The Diagnostic Symptom Matrix (p. 38):** A troubleshooting reference connecting common failures such as incorrect joins, missing filters and query timeouts to potential causes and corrective actions.
- **Must-Have Administrative Rules (p. 39):** Separating essential workspace security and governance configurations from recommended developer defaults.
- **Day-Two Operations Runbook (p. 40):** A recurring maintenance workflow covering query history reviews, negative feedback analysis and Unity Catalog metadata maintenance.

## References and Supporting Resources

The handbook is backed by official documentation, external research and runnable examples.

**1. Master References Index (Page 42)**

The handbook includes a dedicated reference section covering relevant Databricks documentation and supporting technical resources, including:

- Databricks Assistant documentation.
- Unity Catalog informational constraints.
- Unity Catalog row filters and column masks.
- System tables and query history documentation.
- Serverless SQL warehouse scaling and timeout configurations.
- MLflow GenAI agent evaluation.
- Databricks Asset Bundles specifications.

**2. External Research and Related Concepts**

Relevant concept cards include clickable external references to research and technical literature covering transformer attention mechanisms, context dilution and SQL code generation.

**3. Runnable Companion Labs**

Every chapter includes a *Try It Yourself* section with links to the relevant implementation files.

The objective is to make the concepts practical. You should be able to move from understanding an architectural decision to deploying the corresponding example in your own Databricks workspace and examining the results.

This handbook is not meant to be another theoretical introduction to AI agents. It is a collection of engineering practices, experiments and implementation patterns that I want to make easier for other Databricks practitioners to explore and build upon.

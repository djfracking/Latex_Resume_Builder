# Resume workflow (used by the agent)

Inputs in this folder:
- `template.tex` — the LaTeX resume template (entry point, `\input`s the source files)
- `source/` — reusable source material (profile, experience, skills as .tex fragments)
- `job-description.txt` — the posting this resume is being tailored for

Workflow:
1. Read the job description and pick the most relevant bullets from `source/`.
2. Rewrite `source/*.tex` fragments (or `template.tex`) to match the posting.
3. Keep it to one page; compile with pdflatex and check the rendered PDF.
4. Never invent experience — only rephrase and select what is in `source/`.

# Tyrex IHM review prototype

Standalone React + ECharts interface for design review. All market series, orders, datasets, research results and jobs are synthetic. It has no Python API, provider connection or trading control. The application is not Step 3/4 integration or qualification evidence.

Run from this directory:

    npm install
    npm run dev

Open http://127.0.0.1:4173. Production preview: npm run build, then npm run preview.

Check the visualization fixture semantics with node validate-fixtures.mjs. The production build also checks TypeScript.

Check custom window filters with node validate-data-filters.mjs. CUSTOM_FILTERS_BACKEND_NOTE.md describes the user-requested outcome, clock/time-zone, weekday and reusable-recipe contract additions.

Start with Research → Explorer. Select a configuration and window; inspect the synchronized Up and Down charts, event markers, action strip, explanation drawer, outcomes and distributions. Compare the settlement adapter with ask70's book/threshold view.

Data → Warehouse now has a separate review sketch: date/source filters, availability tracks, evidence inspectors, preparation demo, goal-driven window-set builder, saved membership and an experiment draft that receives that set. See DATA_LIBRARY_REVIEW.md and DATA_LIBRARY_BACKEND_PROMPT.md. Its 42 illustrative windows are separate from the Research Explorer's eight-window result fixture. Custom window sets use filters first and individual picks optionally.

The local source notes in EXPLORER_MAPPING.md document chart meanings and the remaining integration work. Browser review state uses sessionStorage only; reset from the header. No original notebook, Python code, evidence, ledger or configuration is changed.


-- a step may depend on any step of its plan, cycles refused; v_step_order: the order to work in
-- Plan P-0001 step 216 (FIX-30). A step added after the plan was built takes the next number and may still have to
-- run before earlier-numbered steps. tr_dep_order and tr_dep_order_upd refuse only a dependency on the step itself,
-- on another plan's step, or one that closes a cycle; tr_step_renumber refuses only moving a step with dependencies
-- to another plan. v_step_order puts each step behind every step it waits for and keeps the numbers' order
-- otherwise. migrate creates the triggers and the view with the shipped schema; no table and no row changes (the
-- dependencies already recorded all point at earlier steps, so none forms a cycle).

SELECT 1;

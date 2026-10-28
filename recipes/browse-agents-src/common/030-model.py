# ---- store contract ------------------------------------------------------
#
# The view code in common/ never touches an agent's storage. Everything it
# needs comes through the plain module-level functions below, which each
# agent's 025-store / 045-tail define (DESIGN.md "Store contract" is the
# normative description). Keys are opaque here except for ``record_key``,
# a dense 0-based ordinal in transcript order that indexes the cached
# ``_TreeData.records`` list.
#
#   identity      store_name()
#   startup       store_init()  store_pop_args()  store_resolve_positional(arg)
#                 store_reset()
#   projects      store_list_projects()  store_list_sessions(project_key)
#                 store_project_path(project_key)  store_project_preview(project_key)
#                 store_project_of_session(session_key)
#   sessions      store_session_exists(session_key)  store_session_item(session_key)
#                 store_session_info(session_key)  store_session_cwd_and_root(session_key)
#                 store_context_dirs(project_key, session_key)
#   children      store_child_sessions(session_key)
#                 store_child_session(session_key, child_key)
#                 store_child_source_path(session_key, child_key)
#                 store_parent_of_child(session_key)
#                 store_resolve_spawned_child(session_key, tool_result, tool_input)
#   source        store_source_path(session_key)
#                 store_source_locator(session_key, record_key)
#                 store_source_excerpt({session_key: [record_key, …]})
#   tail          store_tailable(session_key)  store_tail_open(session_key)
#                 store_tail_poll(session_key, state)  store_tail_interval()

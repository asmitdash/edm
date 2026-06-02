"""SOP Generator: form + chatbot intake -> retrieval -> synthesis -> SOP + skills file.

Public surface:
  - sessions.create_session, advance_session, attach_form, append_chat_message
  - synth.generate_sop_for_session  (retrieval + LLM synthesis + persistence)
  - skills.build_skills_file        (executable JSON action graph from a generated SOP)
  - corpus.upload_sop, list_sops, load_seed_library
"""

# SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
# SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL

find_package(Qt${QT_MAJOR_VERSION} COMPONENTS Test QUIET)
if(NOT TARGET Qt${QT_MAJOR_VERSION}::Test)
    message(STATUS "Qt${QT_MAJOR_VERSION}::Test not found; skipping Bang On Pro C++ QtTest targets.")
    return()
endif()

# 1. Native AI Timeline Command API & Local IPC Server Tests (14 test cases)
add_executable(bangonpro_test_timeline_command_engine
    ${CMAKE_CURRENT_SOURCE_DIR}/../timeline_api/TimelineCommandEngine.cpp
    ${CMAKE_CURRENT_SOURCE_DIR}/../timeline_api/TimelineCommandServer.cpp
    ${CMAKE_CURRENT_SOURCE_DIR}/../timeline_api/tests/test_timeline_command_engine.cpp
)
set_target_properties(bangonpro_test_timeline_command_engine PROPERTIES
    CXX_STANDARD 17
    CXX_STANDARD_REQUIRED ON
    CXX_EXTENSIONS OFF
    AUTOMOC ON
)
target_include_directories(bangonpro_test_timeline_command_engine PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/..
    ${CMAKE_CURRENT_SOURCE_DIR}/../timeline_api
)
target_compile_definitions(bangonpro_test_timeline_command_engine PRIVATE
    QT_NO_CAST_FROM_ASCII
    QT_NO_CAST_TO_ASCII
    QT_NO_URL_CAST_FROM_STRING
    QT_NO_KEYWORDS
)
target_link_libraries(bangonpro_test_timeline_command_engine PRIVATE
    Qt${QT_MAJOR_VERSION}::Core
    Qt${QT_MAJOR_VERSION}::Network
    Qt${QT_MAJOR_VERSION}::Test
)
add_test(NAME bangonpro_test_timeline_command_engine COMMAND bangonpro_test_timeline_command_engine)

# 2. AI Assistant Dock & Premiere Workspace Layout Tests
add_executable(bangonpro_test_ai_assistant_dock
    ${CMAKE_CURRENT_SOURCE_DIR}/../ui/AiAssistantDock.cpp
    ${CMAKE_CURRENT_SOURCE_DIR}/../ui/BangOnProWorkspace.cpp
    ${CMAKE_CURRENT_SOURCE_DIR}/../ui/BangOnProTheme.cpp
    ${CMAKE_CURRENT_SOURCE_DIR}/test_ai_assistant_dock.cpp
)
set_target_properties(bangonpro_test_ai_assistant_dock PROPERTIES
    CXX_STANDARD 17
    CXX_STANDARD_REQUIRED ON
    CXX_EXTENSIONS OFF
    AUTOMOC ON
)
target_include_directories(bangonpro_test_ai_assistant_dock PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/..
    ${CMAKE_CURRENT_SOURCE_DIR}/../ui
)
target_compile_definitions(bangonpro_test_ai_assistant_dock PRIVATE
    QT_NO_CAST_FROM_ASCII
    QT_NO_CAST_TO_ASCII
    QT_NO_URL_CAST_FROM_STRING
    QT_NO_KEYWORDS
)
target_link_libraries(bangonpro_test_ai_assistant_dock PRIVATE
    Qt${QT_MAJOR_VERSION}::Core
    Qt${QT_MAJOR_VERSION}::Widgets
    Qt${QT_MAJOR_VERSION}::Test
    KF${KF_MAJOR}::CoreAddons
)
add_test(NAME bangonpro_test_ai_assistant_dock COMMAND bangonpro_test_ai_assistant_dock)

# 3. Premiere Preset & Effects Browser Widget Tests
add_executable(bangonpro_test_premiere_preset_browser
    ${CMAKE_CURRENT_SOURCE_DIR}/../effects_browser/PremierePresetBrowser.cpp
    ${CMAKE_CURRENT_SOURCE_DIR}/test_premiere_preset_browser.cpp
)
set_target_properties(bangonpro_test_premiere_preset_browser PROPERTIES
    CXX_STANDARD 17
    CXX_STANDARD_REQUIRED ON
    CXX_EXTENSIONS OFF
    AUTOMOC ON
)
target_include_directories(bangonpro_test_premiere_preset_browser PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/..
    ${CMAKE_CURRENT_SOURCE_DIR}/../effects_browser
)
target_compile_definitions(bangonpro_test_premiere_preset_browser PRIVATE
    QT_NO_CAST_FROM_ASCII
    QT_NO_CAST_TO_ASCII
    QT_NO_URL_CAST_FROM_STRING
    QT_NO_KEYWORDS
)
target_link_libraries(bangonpro_test_premiere_preset_browser PRIVATE
    Qt${QT_MAJOR_VERSION}::Core
    Qt${QT_MAJOR_VERSION}::Widgets
    Qt${QT_MAJOR_VERSION}::Test
)
add_test(NAME bangonpro_test_premiere_preset_browser COMMAND bangonpro_test_premiere_preset_browser)

# 4. Pro UI Theme, 9 Workspaces, Shortcut Presets, Transcript & Consent Dialogs Tests
add_executable(bangonpro_test_pro_workspace
    ${CMAKE_CURRENT_SOURCE_DIR}/../ui/BangOnProTheme.cpp
    ${CMAKE_CURRENT_SOURCE_DIR}/../ui/BangOnProWorkspace.cpp
    ${CMAKE_CURRENT_SOURCE_DIR}/../ui/BangOnProShortcutPresets.cpp
    ${CMAKE_CURRENT_SOURCE_DIR}/../ui/TranscriptEditorWidget.cpp
    ${CMAKE_CURRENT_SOURCE_DIR}/../ui/ShowProfileEditorDialog.cpp
    ${CMAKE_CURRENT_SOURCE_DIR}/../ui/ReferenceStyleDialog.cpp
    ${CMAKE_CURRENT_SOURCE_DIR}/../ui/ExternalProviderConsentDialog.cpp
    ${CMAKE_CURRENT_SOURCE_DIR}/../ui/ModelManagerDialog.cpp
    ${CMAKE_CURRENT_SOURCE_DIR}/../ui/AiAssistantDock.cpp
    ${CMAKE_CURRENT_SOURCE_DIR}/test_pro_workspace.cpp
)
set_target_properties(bangonpro_test_pro_workspace PROPERTIES
    CXX_STANDARD 17
    CXX_STANDARD_REQUIRED ON
    CXX_EXTENSIONS OFF
    AUTOMOC ON
)
target_include_directories(bangonpro_test_pro_workspace PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/..
    ${CMAKE_CURRENT_SOURCE_DIR}/../ui
)
target_compile_definitions(bangonpro_test_pro_workspace PRIVATE
    QT_NO_CAST_FROM_ASCII
    QT_NO_CAST_TO_ASCII
    QT_NO_URL_CAST_FROM_STRING
    QT_NO_KEYWORDS
)
target_link_libraries(bangonpro_test_pro_workspace PRIVATE
    Qt${QT_MAJOR_VERSION}::Core
    Qt${QT_MAJOR_VERSION}::Widgets
    Qt${QT_MAJOR_VERSION}::Test
    KF${KF_MAJOR}::CoreAddons
)
add_test(NAME bangonpro_test_pro_workspace COMMAND bangonpro_test_pro_workspace)

# 5. Stage Two Rotoscoping Canvas & ArtCraft 2D/Storyboard Studio Dock Tests
add_executable(bangonpro_test_roto_studio
    ${CMAKE_CURRENT_SOURCE_DIR}/../roto_ui/RotoMaskCanvasWidget.cpp
    ${CMAKE_CURRENT_SOURCE_DIR}/../roto_ui/RotoStudioDock.cpp
    ${CMAKE_CURRENT_SOURCE_DIR}/test_roto_studio.cpp
)
set_target_properties(bangonpro_test_roto_studio PROPERTIES
    CXX_STANDARD 17
    CXX_STANDARD_REQUIRED ON
    CXX_EXTENSIONS OFF
    AUTOMOC ON
)
target_include_directories(bangonpro_test_roto_studio PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/..
    ${CMAKE_CURRENT_SOURCE_DIR}/../roto_ui
)
target_compile_definitions(bangonpro_test_roto_studio PRIVATE
    QT_NO_CAST_FROM_ASCII
    QT_NO_CAST_TO_ASCII
    QT_NO_URL_CAST_FROM_STRING
    QT_NO_KEYWORDS
)
target_link_libraries(bangonpro_test_roto_studio PRIVATE
    Qt${QT_MAJOR_VERSION}::Core
    Qt${QT_MAJOR_VERSION}::Widgets
    Qt${QT_MAJOR_VERSION}::Test
)
add_test(NAME bangonpro_test_roto_studio COMMAND bangonpro_test_roto_studio)

# 6. V1.2 Agent Control Centre (AI Production Department UI)
add_executable(bangonpro_test_agent_control_centre
    ${CMAKE_CURRENT_SOURCE_DIR}/../ui/AgentControlCentre.cpp
    ${CMAKE_CURRENT_SOURCE_DIR}/../ui/BangOnProTheme.cpp
    ${CMAKE_CURRENT_SOURCE_DIR}/../ui/ModelManagerDialog.cpp
    ${CMAKE_CURRENT_SOURCE_DIR}/test_agent_control_centre.cpp
)
set_target_properties(bangonpro_test_agent_control_centre PROPERTIES
    CXX_STANDARD 17
    CXX_STANDARD_REQUIRED ON
    CXX_EXTENSIONS OFF
    AUTOMOC ON
)
target_include_directories(bangonpro_test_agent_control_centre PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}/..
    ${CMAKE_CURRENT_SOURCE_DIR}/../ui
)
target_compile_definitions(bangonpro_test_agent_control_centre PRIVATE
    QT_NO_CAST_FROM_ASCII
    QT_NO_CAST_TO_ASCII
    QT_NO_URL_CAST_FROM_STRING
    QT_NO_KEYWORDS
)
target_link_libraries(bangonpro_test_agent_control_centre PRIVATE
    Qt${QT_MAJOR_VERSION}::Core
    Qt${QT_MAJOR_VERSION}::Widgets
    Qt${QT_MAJOR_VERSION}::Test
)
add_test(NAME bangonpro_test_agent_control_centre COMMAND bangonpro_test_agent_control_centre)

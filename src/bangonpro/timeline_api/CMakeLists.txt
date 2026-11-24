# SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
# SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL

cmake_minimum_required(VERSION 3.16)

set(_BANGONPRO_TIMELINE_API_STANDALONE OFF)
if(CMAKE_SOURCE_DIR STREQUAL CMAKE_CURRENT_SOURCE_DIR)
    set(_BANGONPRO_TIMELINE_API_STANDALONE ON)
    project(BangOnProTimelineApi VERSION 0.1.0 LANGUAGES CXX)
    include(CTest)
endif()

option(BANGONPRO_WITH_KDENLIVE_CORE "Compile KdenliveTimelineBridge against Kdenlive core models" OFF)

# Dual Qt5 (5.15+ / KF5 on Ubuntu 24.04) and Qt6 (6.5+ / KF6 on macOS Apple Silicon) selection
if(NOT DEFINED QT_MAJOR_VERSION)
    find_package(Qt6 COMPONENTS Core Network QUIET)
    if(TARGET Qt6::Core AND TARGET Qt6::Network)
        set(QT_MAJOR_VERSION 6)
    else()
        find_package(Qt5 5.15 COMPONENTS Core Network QUIET)
        if(TARGET Qt5::Core AND TARGET Qt5::Network)
            set(QT_MAJOR_VERSION 5)
        endif()
    endif()
endif()

if(NOT QT_MAJOR_VERSION)
    message(FATAL_ERROR "Bang On Pro Timeline API requires Qt 5.15+ or Qt 6.5+ (Core, Network).")
endif()

find_package(Qt${QT_MAJOR_VERSION} REQUIRED COMPONENTS Core Network)

set(BANGONPRO_TIMELINE_API_HEADERS
    ${CMAKE_CURRENT_SOURCE_DIR}/TimelineCommandEngine.h
    ${CMAKE_CURRENT_SOURCE_DIR}/TimelineCommandServer.h
    ${CMAKE_CURRENT_SOURCE_DIR}/KdenliveTimelineBridge.h
)

set(BANGONPRO_TIMELINE_API_SOURCES
    ${CMAKE_CURRENT_SOURCE_DIR}/TimelineCommandEngine.cpp
    ${CMAKE_CURRENT_SOURCE_DIR}/TimelineCommandServer.cpp
)

if(BANGONPRO_WITH_KDENLIVE_CORE)
    list(APPEND BANGONPRO_TIMELINE_API_SOURCES
        ${CMAKE_CURRENT_SOURCE_DIR}/KdenliveTimelineBridge.cpp
    )
endif()

if(NOT _BANGONPRO_TIMELINE_API_STANDALONE)
    set(BANGONPRO_TIMELINE_API_HEADERS ${BANGONPRO_TIMELINE_API_HEADERS} PARENT_SCOPE)
    set(BANGONPRO_TIMELINE_API_SOURCES ${BANGONPRO_TIMELINE_API_SOURCES} PARENT_SCOPE)
endif()

add_library(bangonpro_timeline_api STATIC
    ${BANGONPRO_TIMELINE_API_SOURCES}
    ${BANGONPRO_TIMELINE_API_HEADERS}
)

set_target_properties(bangonpro_timeline_api PROPERTIES
    CXX_STANDARD 17
    CXX_STANDARD_REQUIRED ON
    CXX_EXTENSIONS OFF
    AUTOMOC ON
    POSITION_INDEPENDENT_CODE ON
)

target_include_directories(bangonpro_timeline_api
    PUBLIC
        ${CMAKE_CURRENT_SOURCE_DIR}
        ${CMAKE_CURRENT_SOURCE_DIR}/..
)

target_compile_definitions(bangonpro_timeline_api
    PRIVATE
        QT_NO_CAST_FROM_ASCII
        QT_NO_CAST_TO_ASCII
        QT_NO_URL_CAST_FROM_STRING
        QT_NO_KEYWORDS
)

if(BANGONPRO_WITH_KDENLIVE_CORE)
    target_compile_definitions(bangonpro_timeline_api PUBLIC BANGONPRO_WITH_KDENLIVE_CORE)
endif()

if(CMAKE_CXX_COMPILER_ID MATCHES "GNU|Clang|AppleClang")
    target_compile_options(bangonpro_timeline_api PRIVATE -Wall -Wextra)
endif()

target_link_libraries(bangonpro_timeline_api
    PUBLIC
        Qt${QT_MAJOR_VERSION}::Core
        Qt${QT_MAJOR_VERSION}::Network
)

if(BUILD_TESTING)
    find_package(Qt${QT_MAJOR_VERSION} COMPONENTS Test QUIET)
    if(TARGET Qt${QT_MAJOR_VERSION}::Test)
        add_executable(test_timeline_command_engine
            ${CMAKE_CURRENT_SOURCE_DIR}/tests/test_timeline_command_engine.cpp
        )
        set_target_properties(test_timeline_command_engine PROPERTIES
            CXX_STANDARD 17
            CXX_STANDARD_REQUIRED ON
            CXX_EXTENSIONS OFF
            AUTOMOC ON
        )
        target_compile_definitions(test_timeline_command_engine
            PRIVATE
                QT_NO_CAST_FROM_ASCII
                QT_NO_CAST_TO_ASCII
                QT_NO_URL_CAST_FROM_STRING
                QT_NO_KEYWORDS
        )
        if(CMAKE_CXX_COMPILER_ID MATCHES "GNU|Clang|AppleClang")
            target_compile_options(test_timeline_command_engine PRIVATE -Wall -Wextra)
        endif()
        target_link_libraries(test_timeline_command_engine
            PRIVATE
                bangonpro_timeline_api
                Qt${QT_MAJOR_VERSION}::Core
                Qt${QT_MAJOR_VERSION}::Network
                Qt${QT_MAJOR_VERSION}::Test
        )
        add_test(NAME test_timeline_command_engine COMMAND test_timeline_command_engine)
    endif()
endif()

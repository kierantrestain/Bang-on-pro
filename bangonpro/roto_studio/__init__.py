"""Bang On Pro Stage Two — Rotoscoping, Masking & Selective ArtCraft 2D Studio.

Provides native AI-assisted & manual rotoscoping/masking, bidirectional tracking
with occlusion handling, hair/edge refinement, MLT/Kdenlive matte & spline export,
and license-clean selective adaptations of ArtCraft (MIT / Apache-2.0) 2D layer
composition, storyboard animatics, and consent-gated asset generation.

SPDX-FileCopyrightText: 2026 Bang On Pro Contributors
SPDX-License-Identifier: GPL-3.0-only OR LicenseRef-KDE-Accepted-GPL
"""

from .artcraft_bridge import (
    ARTCRAFT_ATTRIBUTION_NOTICE,
    ArtCraftLayer,
    Canvas2DComposer,
    GenerativeConsentError,
    GenerativeModelSpec,
    StoryboardEngine,
    StoryboardShot,
    inpaint_masked_region,
    list_generative_models,
    normalize_matte_mask,
    pack_collage_rects,
    point_in_polygon,
)
from .mask_engine import (
    PropagationFrameResult,
    SegmentationBackendInfo,
    combine_masks,
    evaluate_segmentation_backends,
    export_mask_to_mlt,
    interpolate_keyframed_vertices,
    propagate_masks,
    rasterize_mask_shape,
    render_preview,
    sample_bezier_polygon,
    segment_frame,
)
from .models import (
    BrushStroke,
    MaskShape,
    RotoSession,
    validate_roto_session,
)

__all__ = [
    "ARTCRAFT_ATTRIBUTION_NOTICE",
    "ArtCraftLayer",
    "BrushStroke",
    "Canvas2DComposer",
    "GenerativeConsentError",
    "GenerativeModelSpec",
    "MaskShape",
    "PropagationFrameResult",
    "RotoSession",
    "SegmentationBackendInfo",
    "StoryboardEngine",
    "StoryboardShot",
    "combine_masks",
    "evaluate_segmentation_backends",
    "export_mask_to_mlt",
    "inpaint_masked_region",
    "interpolate_keyframed_vertices",
    "list_generative_models",
    "normalize_matte_mask",
    "pack_collage_rects",
    "point_in_polygon",
    "propagate_masks",
    "rasterize_mask_shape",
    "render_preview",
    "sample_bezier_polygon",
    "segment_frame",
    "validate_roto_session",
]

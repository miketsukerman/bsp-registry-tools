"""
Recommendation engine for BSP Assistant: multi-criteria scoring,
feature matching, risk assessment, and alternative discovery.
"""

import logging
from typing import Dict, List, Optional, Set, Tuple

from .models import (
    CompatibilityReport,
    DeviceInfo,
    PresetInfo,
    Recommendation,
    RegistryData,
    RiskAssessment,
    UserRequirements,
)
from .registry_parser import (
    KNOWN_DEV_RELEASES,
    KNOWN_EOL_RELEASES,
    KNOWN_LTS_RELEASES,
    RegistryParser,
)

logger = logging.getLogger(__name__)


class RecommendationEngine:
    """
    Intelligent recommendation and compatibility engine for BSP presets.

    Evaluates user requirements against registry presets using multi-criteria
    scoring, identifies risks, and suggests optimal configurations.
    """

    def __init__(
        self,
        registry_data: Optional[RegistryData] = None,
        registry_parser: Optional[RegistryParser] = None,
    ) -> None:
        if registry_data is not None:
            self.registry_data = registry_data
        elif registry_parser is not None:
            self.registry_data = registry_parser.registry_data
        else:
            raise ValueError("Either registry_data or registry_parser must be provided.")

        self._devices_by_slug: Dict[str, DeviceInfo] = {
            d.slug.lower(): d for d in self.registry_data.devices
        }
        self._features_by_slug = {
            f.slug.lower(): f for f in self.registry_data.features
        }

    def _normalize(self, text: str) -> str:
        """Normalize string for fuzzy matching (lowercase, no hyphens/underscores)."""
        return text.lower().replace("-", "").replace("_", "").strip()

    def _find_preset(self, preset_name: str) -> Optional[PresetInfo]:
        """
        Find a preset by exact name, normalized name, or expanded name ({name}-{release}).
        """
        name_lower = preset_name.lower()
        norm_name = self._normalize(preset_name)

        # 1. Exact match
        for p in self.registry_data.presets:
            if p.name.lower() == name_lower:
                return p

        # 2. Match expanded preset name: {p.name}-{release}
        for p in self.registry_data.presets:
            for r in p.releases:
                expanded = f"{p.name}-{r}".lower()
                if expanded == name_lower or self._normalize(expanded) == norm_name:
                    return p

        # 3. Normalized match
        for p in self.registry_data.presets:
            if self._normalize(p.name) == norm_name:
                return p

        # 4. Substring match
        for p in self.registry_data.presets:
            if norm_name in self._normalize(p.name) or self._normalize(p.name) in norm_name:
                return p

        return None

    def _match_device(self, board_name: Optional[str], device_slug: str) -> Tuple[float, str]:
        """
        Score device match between requested board name and preset device.

        Returns:
            Tuple of (score from 0 to 30, matching note).
        """
        if not board_name:
            return 30.0, "Any compatible board"

        norm_board = self._normalize(board_name)
        norm_device = self._normalize(device_slug)

        device_info = self._devices_by_slug.get(device_slug.lower())
        device_name = device_info.name if device_info else device_slug

        if norm_board == norm_device:
            return 30.0, f"Device '{device_name}' exact match"
        if norm_board in norm_device or norm_device in norm_board:
            return 25.0, f"Device '{device_name}' partial match"

        # Check vendor or SoC match
        if device_info:
            if norm_board in self._normalize(device_info.vendor) or norm_board in self._normalize(device_info.soc_family):
                return 15.0, f"Device '{device_name}' matched by vendor/SoC"

        return 0.0, f"Device mismatch ('{board_name}' != '{device_name}')"

    def _match_features(
        self,
        requested_features: List[str],
        preset_features: List[str],
        device_slug: str,
    ) -> Tuple[float, List[str], List[str]]:
        """
        Score feature compatibility.

        Returns:
            Tuple of (score from 0 to 35, matched_features, missing_features).
        """
        if not requested_features:
            return 35.0, [], []

        norm_preset_features = {f.lower(): f for f in preset_features}
        matched: List[str] = []
        missing: List[str] = []

        device_info = self._devices_by_slug.get(device_slug.lower())
        supported_by_device = (
            {f.lower() for f in device_info.supported_features}
            if device_info else set()
        )

        for req in requested_features:
            req_lower = req.lower()
            if req_lower in norm_preset_features:
                matched.append(req)
            elif req_lower in supported_by_device:
                # Device supports it even if preset doesn't currently include it
                matched.append(req)
            else:
                missing.append(req)

        ratio = len(matched) / len(requested_features) if requested_features else 1.0
        score = 30.0 * ratio

        # Bonus for full match
        if not missing and matched:
            score += 5.0

        return max(0.0, min(35.0, score)), matched, missing

    def _match_release(
        self,
        preference: str,
        specific_version: Optional[str],
        preset: PresetInfo,
    ) -> Tuple[float, str]:
        """
        Score release compatibility and maturity.

        Returns:
            Tuple of (score from 0 to 25, chosen_release).
        """
        releases = preset.releases
        if not releases:
            return 10.0, "default"

        chosen = releases[0]
        score = 0.0

        if preference == "lts":
            # Prefer LTS releases
            lts_found = None
            for r in releases:
                if r.lower() in KNOWN_LTS_RELEASES:
                    lts_found = r
                    break
            if lts_found:
                chosen = lts_found
                score = 25.0
            else:
                chosen = releases[0]
                score = 15.0

        elif preference == "specific" and specific_version:
            norm_ver = specific_version.lower()
            match_found = None
            for r in releases:
                if r.lower() == norm_ver or norm_ver in r.lower():
                    match_found = r
                    break
            if match_found:
                chosen = match_found
                score = 25.0
            else:
                score = 5.0

        else:  # "latest" or default
            # Prefer latest non-EOL, non-dev release or the last release listed
            stable_releases = [
                r for r in releases
                if r.lower() not in KNOWN_EOL_RELEASES and r.lower() not in KNOWN_DEV_RELEASES
            ]
            if stable_releases:
                chosen = stable_releases[-1]
                score = 25.0
            else:
                chosen = releases[-1]
                score = 15.0

        # Adjust for preset stability status
        if preset.stability_status == "eol":
            score = max(0.0, score - 15.0)
        elif preset.stability_status == "development":
            score = max(0.0, score - 5.0)

        return score, chosen

    def score_preset(self, preset: PresetInfo, requirements: UserRequirements) -> float:
        """
        Calculate a multi-criteria compatibility score (0-100) for a preset.

        Criteria:
        - Device match: up to 30 pts
        - Feature compatibility: up to 35 pts
        - Release preference & maturity: up to 25 pts
        - Tested combinations: up to 10 pts
        """
        device_score, _ = self._match_device(requirements.board_name, preset.device)

        # If user explicitly requested a board and it doesn't match at all, heavily penalize
        if requirements.board_name and device_score == 0.0:
            return 0.0

        feature_score, _, _ = self._match_features(
            requirements.features, preset.features, preset.device
        )
        release_score, _ = self._match_release(
            requirements.release_preference,
            requirements.release_version,
            preset,
        )

        test_score = 10.0 if preset.tested_combinations else 0.0

        total = device_score + feature_score + release_score + test_score
        return float(max(0.0, min(100.0, round(total, 1))))

    def recommend(self, requirements: UserRequirements) -> Recommendation:
        """
        Recommend the optimal preset for the given user requirements.

        Args:
            requirements: User requirements specifying board, features, release preference.

        Returns:
            Recommendation dataclass with reasoning, confidence, and alternatives.
        """
        if not self.registry_data.presets:
            return Recommendation(
                preset_name="none-available",
                confidence_score=0.0,
                reasoning=["No presets found in registry"],
                alternatives=[],
                known_issues=["Registry is empty"],
                build_time_estimate="N/A",
            )

        scored: List[Tuple[float, PresetInfo, str, List[str], List[str]]] = []

        for p in self.registry_data.presets:
            score = self.score_preset(p, requirements)
            _, chosen_rel = self._match_release(
                requirements.release_preference, requirements.release_version, p
            )
            _, matched_feats, missing_feats = self._match_features(
                requirements.features, p.features, p.device
            )
            scored.append((score, p, chosen_rel, matched_feats, missing_feats))

        # Sort highest score first
        scored.sort(key=lambda x: x[0], reverse=True)

        best_score, best_preset, chosen_rel, matched_feats, missing_feats = scored[0]

        # Determine effective preset name (include release if multi-release)
        if len(best_preset.releases) > 1 and chosen_rel:
            recommended_name = f"{best_preset.name}-{chosen_rel}"
        else:
            recommended_name = best_preset.name

        # Build reasoning
        device_info = self._devices_by_slug.get(best_preset.device.lower())
        device_name = device_info.name if device_info else best_preset.device
        soc_info = f", {device_info.soc_family}" if (device_info and device_info.soc_family) else ""
        vendor_info = f", {device_info.vendor}" if (device_info and device_info.vendor) else ""

        reasoning: List[str] = [
            f"✓ {device_name} supported ({best_preset.device}{vendor_info}{soc_info})",
        ]

        if matched_feats:
            reasoning.append(f"✓ Features ready: {', '.join(matched_feats)}")
        elif best_preset.features:
            reasoning.append(f"✓ Features included: {', '.join(best_preset.features)}")
        else:
            reasoning.append("✓ Minimal base system configuration")

        if chosen_rel:
            rel_desc = "latest stable" if chosen_rel in KNOWN_LTS_RELEASES or chosen_rel not in KNOWN_DEV_RELEASES else chosen_rel
            reasoning.append(f"✓ Release '{chosen_rel}' selected ({rel_desc})")

        tested_desc = "proven combination" if best_preset.tested_combinations else "compatible configuration"
        reasoning.append(f"✓ Confidence: {best_score:.0f}% ({tested_desc})")

        # Compile known issues / risks
        risk = self.get_risk_assessment(best_preset.name)
        known_issues: List[str] = list(risk.known_issues)
        if risk.warnings:
            known_issues.extend(risk.warnings)
        if missing_feats:
            known_issues.append(f"Missing requested features: {', '.join(missing_feats)}")
        if not known_issues:
            known_issues.append("None for this combination")

        # Alternatives
        alternatives: List[str] = []
        for alt_score, alt_preset, alt_rel, _, _ in scored[1:]:
            if alt_preset.name != best_preset.name or alt_rel != chosen_rel:
                alt_name = f"{alt_preset.name}-{alt_rel}" if len(alt_preset.releases) > 1 else alt_preset.name
                if alt_name not in alternatives and alt_name != recommended_name:
                    alternatives.append(alt_name)
            if len(alternatives) >= 3:
                break

        # Estimate build time
        build_time = "2-3 hours"
        if "isar" in recommended_name.lower():
            build_time = "30-45 minutes"
        elif any(f in ["security", "debug"] for f in best_preset.features):
            build_time = "2.5-3.5 hours"

        return Recommendation(
            preset_name=recommended_name,
            confidence_score=best_score,
            reasoning=reasoning,
            alternatives=alternatives,
            known_issues=known_issues,
            build_time_estimate=build_time,
        )

    def check_compatibility(self, preset: str, features: List[str]) -> CompatibilityReport:
        """
        Check feature compatibility for a specific preset or device.

        Args:
            preset: Preset name or device slug.
            features: List of feature slugs to check.

        Returns:
            CompatibilityReport dataclass.
        """
        found_preset = self._find_preset(preset)
        device_slug = found_preset.device if found_preset else preset
        preset_name = found_preset.name if found_preset else preset
        release = found_preset.releases[0] if (found_preset and found_preset.releases) else ""

        device_info = self._devices_by_slug.get(device_slug.lower())
        supported_by_device = (
            set(device_info.supported_features) if device_info else set()
        )

        supported: List[str] = []
        unsupported: List[str] = []
        reasons: List[str] = []

        for f in features:
            f_norm = f.lower()
            feat_info = self._features_by_slug.get(f_norm)
            if feat_info is None:
                unsupported.append(f)
                reasons.append(f"Feature '{f}' is not defined in the registry")
                continue

            if f_norm in {s.lower() for s in supported_by_device}:
                supported.append(f)
            else:
                unsupported.append(f)
                dev_name = device_info.name if device_info else device_slug
                reasons.append(
                    f"Feature '{f}' is not compatible with device '{dev_name}' "
                    f"(requires vendors: {feat_info.compatible_vendors or 'all'})"
                )

        compatible = len(unsupported) == 0
        if compatible:
            reasons.append("All requested features are compatible with this hardware configuration.")

        return CompatibilityReport(
            compatible=compatible,
            preset_name=preset_name,
            device=device_slug,
            release=release,
            supported_features=supported,
            unsupported_features=unsupported,
            reasons=reasons,
        )

    def find_alternatives(self, preset: str, limit: int = 3) -> List[str]:
        """
        Find alternative presets for the given preset.

        Args:
            preset: Preset name.
            limit: Maximum number of alternatives to return.

        Returns:
            List of alternative preset names.
        """
        target = self._find_preset(preset)
        if not target:
            return []

        alternatives: List[str] = []

        # 1. Same preset with other releases
        for r in target.releases:
            exp_name = f"{target.name}-{r}"
            if exp_name.lower() != preset.lower() and exp_name not in alternatives:
                alternatives.append(exp_name)
            if len(alternatives) >= limit:
                return alternatives

        # 2. Same device, other presets
        for p in self.registry_data.presets:
            if p.name != target.name and p.device == target.device:
                name = f"{p.name}-{p.releases[0]}" if len(p.releases) > 1 else p.name
                if name not in alternatives:
                    alternatives.append(name)
            if len(alternatives) >= limit:
                return alternatives

        # 3. Same vendor, other devices
        target_device = self._devices_by_slug.get(target.device.lower())
        if target_device:
            for p in self.registry_data.presets:
                other_dev = self._devices_by_slug.get(p.device.lower())
                if other_dev and other_dev.vendor == target_device.vendor and p.name != target.name:
                    name = f"{p.name}-{p.releases[0]}" if len(p.releases) > 1 else p.name
                    if name not in alternatives:
                        alternatives.append(name)
                if len(alternatives) >= limit:
                    return alternatives

        return alternatives[:limit]

    def get_risk_assessment(self, preset: str) -> RiskAssessment:
        """
        Evaluate risks associated with building a given preset.

        Checks for untested combinations, EOL releases, missing features, etc.

        Args:
            preset: Preset name.

        Returns:
            RiskAssessment dataclass.
        """
        p = self._find_preset(preset)
        if not p:
            return RiskAssessment(
                risk_level="high",
                untested_combinations=True,
                eol_releases=False,
                known_issues=[f"Preset '{preset}' not found in registry"],
                missing_features=[],
                warnings=[f"Unknown preset: {preset}"],
            )

        untested = not p.tested_combinations
        eol = p.stability_status == "eol"
        dev = p.stability_status == "development"

        warnings: List[str] = []
        known_issues: List[str] = []

        if eol:
            warnings.append("Release is End-of-Life (EOL) and receives no upstream security updates.")
        if dev:
            warnings.append("Development/bleeding-edge release may have unstable APIs or breakages.")
        if untested:
            warnings.append("This combination has not been verified in automated hardware test suites.")

        # Risk level determination
        if eol:
            risk_level = "high"
        elif dev:
            risk_level = "medium"
        else:
            risk_level = "low"

        return RiskAssessment(
            risk_level=risk_level,  # type: ignore[arg-type]
            untested_combinations=untested,
            eol_releases=eol,
            known_issues=known_issues,
            missing_features=[],
            warnings=warnings,
        )

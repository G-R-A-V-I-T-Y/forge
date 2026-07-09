"""Event calendar for M7b — macro calendar, token unlocks, scheduled events for market impact."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from store.ledger import read_partition

EVENT_CALENDAR_DIR = Path(__file__).resolve().parent.parent / "ledger" / "events"
DEFAULT_START_TIME = datetime.now(timezone.utc) - timedelta(days=1)
DEFAULT_END_TIME = datetime.now(timezone.utc) + timedelta(days=365)  # 1 year future events

# Event type constants for filtering and processing
EVENT_TYPE_MACRO = "economic_data_release"
EVENT_TYPE_TOKEN_UNLOCKS = "token_economics"
EVENT_TYPE_NETWORK_EVENTS = "network_events"
EVENT_TYPE_MARKET_STRUCTURE = "market_structure"
EVENT_TYPE_OTHER = "other"

# Event family constants for cross-asset event grouping
EVENT_FAMILY_INFLATION = "inflation"
EVENT_FAMILY_LIQUIDITY = "liquidity"
EVENT_FAMILY_GOVERNANCE = "governance"
EVENT_FAMILY_TECHNOLOGY = "technology"
EVENT_FAMILY_REGULATION = "regulation"


class EventCalendar:
    """Calendar for market-moving events used in M7b."""

    def __init__(self, events_path: Path | None = None):
        self.events_path = events_path or EVENT_CALENDAR_DIR
        self._cache: dict[str, pd.DataFrame] = {}

    def _load_events_partition(self, when: datetime) -> pd.DataFrame:
        """Load events for the given month, with caching."""
        month_key = when.strftime("%Y-%m")
        if month_key not in self._cache:
            try:
                self._cache[month_key] = read_partition(
                    "events",
                    when,
                    self.events_path,
                )
            except Exception:
                # If no partition exists, return empty DataFrame
                self._cache[month_key] = pd.DataFrame()
        return self._cache[month_key]

    def get_events_for_heartbeat(
        self,
        universe: list[str],
        start_time: datetime | None = None,
        end_time: datetime | None = None,
    ) -> dict:
        """Get events for the heartbeat cycle.

        Returns a dict with:
        - asset_events: dict mapping asset to list curent events
        - cross_asset_signals: dict mapping event family to list of events  
        - next_critical_event: the event with earliest scheduled_time
        """
        if start_time is None:
            start_time = DEFAULT_START_TIME
        if end_time is None:
            end_time = DEFAULT_END_TIME

        # Load events for the month containing start_time
        month_start = datetime(start_time.year, start_time.month, 1, tzinfo=timezone.utc)
        events_df = self._load_events_partition(month_start)

        if events_df.empty:
            return {
                "asset_events": {},
                "cross_asset_signals": {},
                "next_critical_event": None,
            }

        # Filter events based on universe and timeframe
        filtered_events = self._filter_events(events_df, universe, start_time, end_time)

        # Organize by asset
        asset_events = {}
        for _, event in filtered_events.iterrows():
            event_dict = event.to_dict()

                # Parse datetime fields
                if "scheduled_time" in event_dict:
                    event_dict["scheduled_time"] = datetime.fromisoformat(
                        event_dict["scheduled_time"].replace("Z", "+00:00")
                    )

                # For features, we need to flatten the event data
                # Create a feature dict that can be used by the agent decision loop
                event_features = self._event_to_features(event_dict)
                if event_features["asset_relevant"] and event_features["asset_relevant"] in universe:
                    asset = event_features["asset_relevant"]
                    if asset not in asset_events:
                        asset_events[asset] = []
                    asset_events[asset].append(event_features)

        # Organize cross-asset signals by event family
        cross_asset_signals = {}
        for _, event in filtered_events.iterrows():
            event_dict = event.to_dict()

            # Parse datetime fields
            if "scheduled_time" in event_dict:
                event_dict["scheduled_time"] = datetime.fromisoformat(
                    event_dict["scheduled_time"].replace("Z", "+00:00")
                )

            for family in event_dict.get("event_family", []):
                if family not in cross_asset_signals:
                    cross_asset_signals[family] = []
                cross_asset_signals[family].append(event_dict)

        # Find next critical event (earliest scheduled time)
        next_critical_event = None
        if not filtered_events.empty:
            next_row = filtered_events.loc[filtered_events["scheduled_time"].idxmin()]
            next_critical_event = next_row.to_dict()
            # Parse datetime field
            if "scheduled_time" in next_critical_event:
                next_critical_event["scheduled_time"] = datetime.fromisoformat(
                    next_critical_event["scheduled_time"].replace("Z", "+00:00")
                )

        return {
            "asset_events": asset_events,
            "cross_asset_signals": cross_asset_signals,
            "next_critical_event": next_critical_event,
        }

    def _event_to_features(self, event: dict) -> dict:
        """Convert event dict to feature format for agent decision loop.
        
        Returns features that can be used by existing heartbeat feature system.
        """
        features = {
            "name": f"event_{event.get('event_id', 'unknown')}",
            "feature": f"event_{event.get('type', 'unknown')}_{event.get('subtype', 'unknown')}",
            "thresholds": [
                {"op": ">", "value": 0.5, "weight": 0.8},  # High probability events
                {"op": "else", "weight": 0.0}
            ],
            "missing": "skip",
        }
        
        # Add event-specific features
        if event.get("asset"):
            features["asset_relevant"] = event["asset"]
        
        # Add event timing features
        if "scheduled_time" in event:
            event_time = datetime.fromisoformat(event["scheduled_time"].replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            hours_to_event = (event_time - now).total_seconds() / 3600
            
            if hours_to_event <= 24:  # Events in next 24 hours
                features["imminent_event"] = 0.9
            elif hours_to_event <= 72:  # Events in next 72 hours
                features["imminent_event"] = 0.7
            else:
                features["imminent_event"] = 0.0
        
        # Add impact level features
        if "expected_impact" in event:
            impact_levels = {
                "high_pos": 0.8,
                "medium_pos": 0.5,
                "low_pos": 0.3,
                "neutral": 0.0,
                "low_neg": -0.3,
                "medium_neg": -0.5,
                "high_neg": -0.8,
            }
            features["impact_level"] = impact_levels.get(event["expected_impact"], 0.0)
        
        return features

    def generate_feature_rows(self, universe: list[str]) -> pd.DataFrame:
        """Generate feature rows for event calendar events.
        
        This creates the same format as heartbeat features for integration.
        """
        rows = []
        
        # Get events for the current month
        now = datetime.now(timezone.utc)
        month_start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
        events_df = self._load_events_partition(month_start)
        
        if events_df.empty:
            return pd.DataFrame()
        
        # Filter events for universe
        filtered_events = self._filter_events(events_df, universe, now, now + timedelta(days=30))
        
        for _, event in filtered_events.iterrows():
            event_dict = event.to_dict()
            
            # Convert event to features
            features = self._event_to_features(event_dict)
            
            # Create feature row structure
            row = {
                "name": features["name"],
                "feature": features["feature"],
                "thresholds": features["thresholds"],
                "missing": features["missing"],
                "asset_relevant": features.get("asset_relevant"),
                "imminent_event": features.get("imminent_event", 0.0),
                "impact_level": features.get("impact_level", 0.0),
                "scheduled_time": event_dict.get("scheduled_time"),
                "event_description": event_dict.get("description", ""),
            }
            
            rows.append(row)
        
        return pd.DataFrame(rows)

    def get_events_summary(self) -> dict:
        """Get summary statistics of events in the calendar."""
        if not self._cache:
            return {"total_events": 0, "event_types": {}, "event_families": {}}

        all_events = []
        for month_df in self._cache.values():
            all_events.extend([row.to_dict() for _, row in month_df.iterrows()])

        if not all_events:
            return {"total_events": 0, "event_types": {}, "event_families": {}}

        summary = {
            "total_events": len(all_events),
            "event_types": {},
            "event_families": {},
            "upcoming_events": 0,
        }

        now = datetime.now(timezone.utc)
        for event in all_events:
            # Count by type
            event_type = event.get("type", "unknown")
            summary["event_types"][event_type] = summary["event_types"].get(event_type, 0) + 1

            # Count by family
            for family in event.get("event_family", []):
                summary["event_families"][family] = summary["event_families"].get(family, 0) + 1

            # Count upcoming events (within next 24 hours)
            scheduled_time = event.get("scheduled_time")
            if scheduled_time:
                try:
                    event_time = datetime.fromisoformat(scheduled_time.replace("Z", "+00:00"))
                    if event_time <= now + timedelta(hours=24):
                        summary["upcoming_events"] += 1
                except Exception:
                    pass

        return summary


# Singleton instance for use throughout Forge
_event_calendar = EventCalendar()


def get_event_calendar() -> EventCalendar:
    """Get the global event calendar instance."""
    return _event_calendar


def read_events_for_heartbeat(
    universe: list[str],
    start_time: datetime | None = None,
    end_time: datetime | None = None,
) -> dict:
    """Convenience function to get events for heartbeat integration.

    This is the function imported and used by `market/heartbeat.py`.
    """
    return _event_calendar.get_events_for_heartbeat(
        universe, start_time, end_time
    )

/**
 * AAW Agentic Studio — Main Application Component
 *
 * Mounts the StudioLayout with live store binding, LeftRail navigation,
 * dynamic view routing based on activeNav, and RightDrawer telemetry console.
 */

import { useEffect } from "react";
import { client } from "./api/client";
import { useEngineStore } from "./store/engineStore";
import { StudioLayout } from "./components/layout/StudioLayout";
import { LeftRail } from "./components/nav/LeftRail";
import { RightDrawer } from "./components/drawer/RightDrawer";
import { LiveCommandCenterView } from "./components/views/LiveCommandCenterView";
import { NewTaskWizardView } from "./components/views/NewTaskWizardView";
import { RunsHistoryView } from "./components/views/RunsHistoryView";
import { SettingsView } from "./components/views/SettingsView";
import { ExperienceView } from "./components/views/ExperienceView";

export function App() {
  const {
    activeNav,
    loadBootstrap,
  } = useEngineStore();

  const token = client.getToken() || "none";

  useEffect(() => {
    // Attempt automatic bootstrap on mount if served by AAW server
    if (token && token !== "none" && token !== "__AAW_TOKEN__") {
      loadBootstrap();
    }
  }, [token, loadBootstrap]);

  // Dynamic View Routing based on LeftRail activeNav state
  const renderCenterView = () => {
    switch (activeNav) {
      case "new-task":
        return <NewTaskWizardView />;
      case "runs":
        return <RunsHistoryView />;
      case "experience":
        return <ExperienceView />;
      case "settings":
        return <SettingsView />;
      case "command-center":
      default:
        return <LiveCommandCenterView />;
    }
  };

  return (
    <StudioLayout
      leftRail={<LeftRail />}
      centerCanvas={renderCenterView()}
      rightDrawer={<RightDrawer />}
    />
  );
}

export default App;

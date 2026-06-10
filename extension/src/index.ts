import { ExtensionContext } from "@foxglove/extension";

import { initPhotoStepperPanel } from "./PhotoStepperPanel";

export function activate(extensionContext: ExtensionContext): void {
  extensionContext.registerPanel({
    name: "photo-stepper",
    initPanel: initPhotoStepperPanel,
  });
}

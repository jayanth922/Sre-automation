import * as React from "react"

import { cn } from "@/lib/utils"

export interface ScrollAreaProps
    extends React.HTMLAttributes<HTMLDivElement> {
    orientation?: "vertical" | "horizontal"
}

const ScrollArea = React.forwardRef<HTMLDivElement, ScrollAreaProps>(
    ({ className, orientation = "vertical", ...props }, ref) => (
        <div
            ref={ref}
            className={cn("overlay-scrollbar relative overflow-auto", className)}
            {...props}
        />
    )
)
ScrollArea.displayName = "ScrollArea"

export { ScrollArea }

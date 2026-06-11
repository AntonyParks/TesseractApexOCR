import mss
import numpy as np
import cv2

MONITOR = 2  # set to the monitor index you use in your OCR script (1 = primary)

start_point = None
end_point = None
dragging = False
current_frame = None


def mouse_callback(event, x, y, flags, param):
    global start_point, end_point, dragging, current_frame

    if event == cv2.EVENT_LBUTTONDOWN:
        start_point = (x, y)
        end_point = (x, y)
        dragging = True

    elif event == cv2.EVENT_MOUSEMOVE and dragging:
        end_point = (x, y)

    elif event == cv2.EVENT_LBUTTONUP:
        dragging = False
        end_point = (x, y)
        if start_point and end_point:
            x1, y1 = start_point
            x2, y2 = end_point
            left = min(x1, x2)
            top = min(y1, y2)
            width = abs(x2 - x1)
            height = abs(y2 - y1)

            print("\nUse this CROP in your OCR script:")
            print(f"CROP = {{")
            print(f"    'left': {left},")
            print(f"    'top': {top},")
            print(f"    'width': {width},")
            print(f"    'height': {height}")
            print(f"}}\n")


def main():
    global current_frame

    with mss.mss() as sct:
        monitor = sct.monitors[MONITOR]
        region = {
            "left": monitor["left"],
            "top": monitor["top"],
            "width": monitor["width"],
            "height": monitor["height"],
        }

        cv2.namedWindow("Select Killfeed Region")
        cv2.setMouseCallback("Select Killfeed Region", mouse_callback)

        print("Drag a box over the killfeed in the window. Press 'q' to quit.")

        while True:
            img = np.array(sct.grab(region))
            current_frame = cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)

            display = current_frame.copy()
            if start_point and end_point:
                cv2.rectangle(display, start_point, end_point, (0, 255, 0), 2)

            cv2.imshow("Select Killfeed Region", display)
            if cv2.waitKey(30) & 0xFF == ord('q'):
                break

        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()

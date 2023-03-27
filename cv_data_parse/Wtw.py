import os
import cv2
from pathlib import Path
import xml.etree.ElementTree as ET
from cv_data_parse.base import DataRegister, DataLoader, DataSaver


class Loader(DataLoader):
    """https://tianchi.aliyun.com/dataset/108587

    Data structure:
        .
        ├── test
        │   ├── images
        │   └── xml     # 3611 items
        └── train
            ├── images
            └── xml     # 10970 items

    Usage:
        .. code-block:: python

            # get data
            from cv_data_parse.Wtw import DataRegister, Loader

            loader = Loader('data/WTW')
            data = loader(data_type=DataRegister.ALL, generator=True, image_type=DataRegister.IMAGE)
            r = next(data[0])

            # visual
            from utils.visualize import ImageVisualize

            image = r['image']
            segmentation = r['segmentation']
            transcription = r['transcription']

            vis_image = np.zeros_like(image) + 255
            vis_image = ImageVisualize.box(vis_image, segmentation)
            vis_image = ImageVisualize.text(vis_image, segmentation, transcription)

    """

    def _call(self, load_type, image_type, **kwargs):
        root_dir = f'{self.data_dir}/{load_type.value}'
        for xml_file in Path(f'{root_dir}/xml').glob('*.xml'):
            tree = ET.parse(xml_file)
            root = tree.getroot()
            assert root.tag == 'annotation', f'pascal voc xml root element should be annotation, rather than {root.tag = }'

            elem = root.find('filename')
            image_path = os.path.abspath(f'{root_dir}/images/{elem.text}')
            if image_type == DataRegister.PATH:
                image = image_path
            elif image_type == DataRegister.IMAGE:
                image = cv2.imread(image_path)
            else:
                raise ValueError(f'Unknown input {image_type = }')

            _id = elem.text

            elem = root.find('size')
            size = {subelem.tag: int(subelem.text) for subelem in elem}
            # width, height, depth
            size = (size['width'], size['height'], size['depth'])

            segmentation = []
            for elem in root.iterfind('object'):
                subelem = elem.find('bndbox')
                segmentation.append([float(subelem.find(value).text) for value in ('xmin', 'ymin', 'xmax', 'ymax')])

            yield dict(
                _id=_id,
                image=image,
                size=size,
                segmentation=segmentation
            )
